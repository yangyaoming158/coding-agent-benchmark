"""排行榜聚合（E7-T0）：谁有资格上榜、多轮怎么合成一行。

    eligible_runs(session, benchmark_set_id=...)   → 有资格的那些运行
    cost_sources(session, run_ids)                 → 每次运行的成本来源构成
    facet_cells(session, run_ids, facet=...)       → 按难度/语言/仓库的分面
    summarize(runs, ...)                           → 纯函数，合成排行榜行

分成"查库"和"纯函数"两半，和 `app.evaluation.progress` 同一个写法：
排名口径要能不起数据库单测，查询要能一次查完不产生 N+1。

## 一、谁有资格上榜

协议只给了两条，都不够：

| 条款 | 挡掉什么 | 落在哪一列 |
|:---|:---|:---|
| C-26 / C-26b | 平台故障率超过 5% → 记 `PARTIAL` | `status` |
| C-28 | 工作区不干净时跑的 → `dirty = true` | `dirty` |

光靠这两条，库里 18 个实验会有 14 个"合格"，其中包括哨兵、诊断用的参赛者、
只跑了 1–2 道题的探测跑，以及 4 个一次模型都没调到的。所以这里一共六条：

1. `status = COMPLETED` —— C-26b 的落点。`PARTIAL` 是"降级"，`RUNNING` 还没跑完。
2. `dirty = false` —— C-28。
3. `leaderboard_excluded_reason IS NULL` —— 人工排除（见 0006 迁移的说明）。
4. `agent_configs.enabled = true` —— 停用的参赛者不上榜。库里的
   `aider@deepseek-chat+autotest` 是一次性诊断，不是选手。
5. `agents.kind` 不是 ORACLE / NOOP / MOCK —— 哨兵是量具不是选手。
   Oracle 永远 100%、Noop 永远 0%，混进榜单只会把榜首和榜尾各占一格。
6. `total_tasks = benchmark_sets.task_count` —— 整份快照都跑了才算数。
   严格解决率的分母是题库总题数（C-21），只跑了 2 道题的探测跑，
   它的 0% 和跑满 22 道的 0% 根本不是一个数。

第 3–6 条协议里没有。它们回答的不是"这次实验跑得对不对"（那是 C-26 的事），
而是"这个数字能不能和别人的放在一起比"。

## 二、为什么按 `(参赛者, 协议版本)` 分组

协议 C-59 写死了：改 5% 门槛要升协议版本，**排行榜要按协议版本分开展示，
不能把不同门槛下的结果混排**。所以协议版本是分组键的一部分，
同一个参赛者跨了两个协议版本就占两行，各自标明版本。

数据集不进分组键 —— 它是**查询参数**。不同数据集的解决率之间没有可比性，
放进同一张榜等于把两场考试的分数排在一起。

## 三、为什么一行要带轮间离散度

`07-platform-architecture.md` §18.6 第六节实测过：同一批题跑两遍，
**五分之一的题会改结论**（aider 22.7%、claude-code 18.2%），单轮解决率
光凭这个抖动就能差 ±9 个百分点。而 MET-01 要求"偏差 ≤5 个百分点" ——
只报一个平均数，那个指标没法解释。所以每行都带 `min` / `max` / `spread` 和轮数。

## 四、成本为什么要单独报"报不出来的次数"

`evaluation_runs.total_cost_usd` 是把报不出成本的 attempt 跳过之后加出来的。
claude-code 走中转端点，44 次全报 `unavailable`（E3-T5 定的规矩），
于是它的成本栏是 `$0.0000` —— 读起来就是"不花钱"，而实际花掉的钱一分不少。
所以行里除了金额，还要带三种成本来源各有多少次，让前端能显示"成本不可用"
而不是显示 0（协议纪律 3 要求 reported / estimated / unavailable 区分显示）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.domain.enums import AgentKind, AgentOutcome, CostSource, EvaluationRunStatus
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.benchmark import BenchmarkSet, BenchmarkTask, Repository
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun

#: 哨兵 Agent 的类型。它们是量具不是选手，不进排行榜。
SENTINEL_KINDS = (AgentKind.ORACLE, AgentKind.NOOP, AgentKind.MOCK)

#: 解决率保留四位小数，和 `evaluation_runs.strict_resolve_rate` 那一列对齐。
_RATE_PLACES = Decimal("0.0001")
#: 金额保留六位，和 `evaluation_task_runs.cost_usd` 对齐。
_COST_PLACES = Decimal("0.000001")


class LeaderboardMetric(StrEnum):
    """排行榜按什么排。名字是 URL 参数里直接用的值。"""

    #: 平均严格解决率，从高到低。默认。
    RESOLVE_RATE = "resolve_rate"
    #: 每题成本，从低到高。报不出成本的排最后。
    COST = "cost"
    #: 平均 makespan，从短到长。
    DURATION = "duration"
    #: 每题 token 用量，从少到多。
    TOKENS = "tokens"


class LeaderboardFacet(StrEnum):
    """按哪个维度分面（§16.2 的 Leaderboard 页要"按难度/语言/仓库分面"）。"""

    DIFFICULTY = "difficulty"
    LANGUAGE = "language"
    REPOSITORY = "repository"


# ── 事实 ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EligibleRun:
    """一次有资格上榜的实验。字段都从 `evaluation_runs` 那一行直接来。"""

    evaluation_run_id: int
    agent_config_id: int
    label: str
    agent_name: str
    agent_display_name: str
    agent_version: str
    model_name: str
    protocol_version: str
    benchmark_set_id: int
    total_tasks: int
    resolved_count: int
    strict_resolve_rate: Decimal | None
    effective_resolve_rate: Decimal | None
    infra_failure_count: int
    retry_count: int
    total_cost_usd: Decimal
    total_tokens: int
    makespan_ms: int | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class CostSourceCount:
    """一次实验里，某种成本来源出现了几次 attempt。"""

    evaluation_run_id: int
    cost_source: CostSource | None
    attempts: int


@dataclass(frozen=True, slots=True)
class FacetCount:
    """一次实验在某个分面取值上的解决情况。"""

    evaluation_run_id: int
    #: 分面取值：`easy` / `zh` / `pallets/click`。枚举原样透出，不做中文映射（AC-10）。
    value: str
    resolved: int
    total: int


@dataclass(frozen=True, slots=True)
class FacetCell:
    """排行榜一行在某个分面取值上的成绩。"""

    value: str
    resolved: int
    total: int
    resolve_rate: Decimal | None


@dataclass(frozen=True, slots=True)
class LeaderboardRow:
    """排行榜的一行 = 一个参赛者 × 一个协议版本。"""

    rank: int
    agent_config_id: int
    label: str
    agent_name: str
    agent_display_name: str
    agent_version: str
    model_name: str
    #: 协议 C-59：不同协议版本的结果不混排，所以它是分组键的一部分。
    protocol_version: str

    #: 合成这一行用了几次实验（几轮）。
    run_count: int
    #: 具体是哪几次，按 id 升序。前端画散点、追证据都要它。
    run_ids: tuple[int, ...]
    #: 每轮的题数（几轮都一样，资格里要求跑满整份快照）。
    tasks_per_run: int

    #: 轮间平均严格解决率（C-21 的口径：RESOLVED 题数 / 题库总题数）。
    resolve_rate_mean: Decimal | None
    resolve_rate_min: Decimal | None
    resolve_rate_max: Decimal | None
    #: 最大减最小。§18.6 第六节实测这个抖动能到 9 个百分点，只报均值会误导。
    resolve_rate_spread: Decimal | None
    #: 轮间平均有效解决率（C-21 的诊断口径，分母是"拿到了可归因于 AI 的结果"的题数）。
    effective_resolve_rate_mean: Decimal | None
    #: 轮间平均解决题数。
    resolved_mean: Decimal

    #: 各轮成本之和。**可能被低估** —— 看下面三个计数。
    cost_usd_total: Decimal
    #: 每题成本 = 总成本 / 总题数（轮数 × 每轮题数）。
    #:
    #: **一次都报不出成本时是 None**；部分报不出时是一个**下界**（只加了报得出的部分），
    #: 同时 `cost_lower_bound` 置 True。理由见 `_cost_per_task()`。
    cost_per_task: Decimal | None
    #: `cost_per_task` 是不是下界（有 attempt 报不出成本）。True 时前端要标 "≥"，
    #: 按成本排名时这样的行排在成本完整的行后面（见 `_sort_key`）。
    cost_lower_bound: bool
    #: 成本来源构成（协议纪律 3 要求三种来源区分显示）。
    cost_reported_attempts: int
    cost_estimated_attempts: int
    #: 报不出成本的 attempt 数。大于 0 时上面那个金额是偏低的，前端要说出来。
    cost_unavailable_attempts: int

    total_tokens: int
    tokens_per_task: int | None
    #: 轮间平均 makespan。取不到（没有一轮记了 makespan）时是 None。
    makespan_ms_mean: int | None

    #: 各轮平台故障题数之和。能进榜说明每一轮都没超 C-26 的 5%，
    #: 但"零故障跑完"和"每轮都擦着门槛过"可信度不同，所以报出来。
    infra_failure_total: int
    #: 各轮重试次数之和（协议 C-56：零重试和重试 30 次凑齐的可信度不一样）。
    retry_total: int

    #: 请求了 `facet` 时才有，按取值排序。没请求就是空。
    facets: tuple[FacetCell, ...]


# ── 查库 ────────────────────────────────────────────────────


def eligible_runs(session: Session, *, benchmark_set_id: int | None = None) -> list[EligibleRun]:
    """有资格上排行榜的实验，按 id 升序。**六条资格见模块开头第一节。**

    一条 SQL 查完。返回的行数等于合格实验数，和排行榜最终有几行无关 ——
    合并成行是 `summarize()` 在内存里做的。
    """
    stmt = (
        sa.select(
            EvaluationRun.id,
            EvaluationRun.agent_config_id,
            AgentConfig.label,
            Agent.name,
            Agent.display_name,
            AgentConfig.agent_version,
            AgentConfig.model_name,
            EvaluationRun.protocol_version,
            EvaluationRun.benchmark_set_id,
            EvaluationRun.total_tasks,
            EvaluationRun.resolved_count,
            EvaluationRun.strict_resolve_rate,
            EvaluationRun.effective_resolve_rate,
            EvaluationRun.infra_failure_count,
            EvaluationRun.retry_count,
            EvaluationRun.total_cost_usd,
            EvaluationRun.total_tokens,
            EvaluationRun.makespan_ms,
            EvaluationRun.finished_at,
        )
        .join(AgentConfig, AgentConfig.id == EvaluationRun.agent_config_id)
        .join(Agent, Agent.id == AgentConfig.agent_id)
        .join(BenchmarkSet, BenchmarkSet.id == EvaluationRun.benchmark_set_id)
        .where(
            # ① C-26b：超标的实验落库记 PARTIAL。直接读这一列而不是在这里重算 5%，
            #    免得同一条规则有两份实现，改了一处忘另一处
            EvaluationRun.status == EvaluationRunStatus.COMPLETED,
            # ② C-28
            EvaluationRun.dirty.is_(False),
            # ③ 人工排除
            EvaluationRun.leaderboard_excluded_reason.is_(None),
            # ④ 停用的参赛者
            AgentConfig.enabled.is_(True),
            # ⑤ 哨兵
            Agent.kind.not_in(SENTINEL_KINDS),
            # ⑥ 跑满整份快照。`task_count > 0` 是防呆：空快照下这个等式会变成
            #    "0 == 0" 恒真，把一个一道题都没投的实验放进来
            BenchmarkSet.task_count > 0,
            EvaluationRun.total_tasks == BenchmarkSet.task_count,
        )
        .order_by(EvaluationRun.id)
    )
    if benchmark_set_id is not None:
        stmt = stmt.where(EvaluationRun.benchmark_set_id == benchmark_set_id)
    return [EligibleRun(*row) for row in session.execute(stmt)]


def cost_sources(session: Session, run_ids: Sequence[int]) -> list[CostSourceCount]:
    """这些实验各自的成本来源构成。一条 SQL。

    数的是 **attempt 数**不是题数：成本累计全部 attempt（C-56），
    重试也是真金白银花掉的。
    """
    if not run_ids:
        return []
    rows = session.execute(
        sa.select(
            EvaluationTaskRun.evaluation_run_id,
            EvaluationTaskRun.cost_source,
            sa.func.count(),
        )
        .where(EvaluationTaskRun.evaluation_run_id.in_(list(run_ids)))
        .group_by(EvaluationTaskRun.evaluation_run_id, EvaluationTaskRun.cost_source)
    ).all()
    return [CostSourceCount(int(run_id), source, int(count)) for run_id, source, count in rows]


def facet_counts(
    session: Session, run_ids: Sequence[int], *, facet: LeaderboardFacet
) -> list[FacetCount]:
    """这些实验按某个维度分面的解决情况。一条 SQL。

    只数 **canonical attempt**（C-24、C-58）：解决率的口径就是认定结果那一次，
    重试的中间过程不算。分母是"这个分面下有几道题拿到了认定结果"，
    对合格实验来说等于该分面的题数 —— 合格要求 `completed_tasks = total_tasks`。
    """
    if not run_ids:
        return []
    # 三个都 cast 成文本：枚举不 cast 的话 group by 出来的是枚举对象，
    # 而分面取值要原样透出成字符串（AC-10）。仓库名 cast 是个空操作，
    # 三条写法一致，省得读的人去想"为什么这一条不一样"
    value: sa.Cast[str]
    if facet is LeaderboardFacet.DIFFICULTY:
        value = sa.cast(BenchmarkTask.difficulty, sa.Text)
    elif facet is LeaderboardFacet.LANGUAGE:
        value = sa.cast(BenchmarkTask.issue_language, sa.Text)
    else:
        value = sa.cast(Repository.full_name, sa.Text)

    stmt = (
        sa.select(EvaluationTaskRun.evaluation_run_id, value)
        .add_columns(
            sa.func.count(),
            sa.func.count().filter(EvaluationTaskRun.agent_outcome == AgentOutcome.RESOLVED),
        )
        .join(BenchmarkTask, BenchmarkTask.id == EvaluationTaskRun.benchmark_task_id)
    )
    if facet is LeaderboardFacet.REPOSITORY:
        stmt = stmt.join(Repository, Repository.id == BenchmarkTask.repository_id)

    rows = session.execute(
        stmt.where(
            EvaluationTaskRun.evaluation_run_id.in_(list(run_ids)),
            EvaluationTaskRun.is_canonical.is_(True),
        )
        .group_by(EvaluationTaskRun.evaluation_run_id, value)
        .order_by(value)
    ).all()
    return [
        FacetCount(int(run_id), str(facet_value), int(resolved), int(total))
        for run_id, facet_value, total, resolved in rows
    ]


# ── 纯函数：合成排行榜 ──────────────────────────────────────


def summarize(
    runs: Sequence[EligibleRun],
    *,
    costs: Sequence[CostSourceCount] = (),
    facets: Sequence[FacetCount] = (),
    metric: LeaderboardMetric = LeaderboardMetric.RESOLVE_RATE,
) -> list[LeaderboardRow]:
    """把合格实验合成排行榜。**纯函数**，同样的输入永远同样的输出。

    分组键是 `(agent_config_id, protocol_version)` —— 协议 C-59 要求
    不同协议版本分开展示。名次在排序之后编，从 1 开始。
    """
    cost_by_run: dict[int, dict[CostSource | None, int]] = {}
    for entry in costs:
        cost_by_run.setdefault(entry.evaluation_run_id, {})[entry.cost_source] = entry.attempts
    facet_by_run: dict[int, list[FacetCount]] = {}
    for cell in facets:
        facet_by_run.setdefault(cell.evaluation_run_id, []).append(cell)

    grouped: dict[tuple[int, str], list[EligibleRun]] = {}
    for run in runs:
        grouped.setdefault((run.agent_config_id, run.protocol_version), []).append(run)

    rows = [_build_row(members, cost_by_run, facet_by_run) for members in grouped.values()]
    rows.sort(key=_sort_key(metric))
    return [_with_rank(row, index + 1) for index, row in enumerate(rows)]


def _build_row(
    members: list[EligibleRun],
    cost_by_run: dict[int, dict[CostSource | None, int]],
    facet_by_run: dict[int, list[FacetCount]],
) -> LeaderboardRow:
    """把同一个参赛者在同一协议版本下的若干轮合成一行。"""
    members = sorted(members, key=lambda r: r.evaluation_run_id)
    head = members[0]
    rates = [r.strict_resolve_rate for r in members if r.strict_resolve_rate is not None]
    effective = [r.effective_resolve_rate for r in members if r.effective_resolve_rate is not None]
    makespans = [r.makespan_ms for r in members if r.makespan_ms is not None]

    tasks_total = sum(r.total_tasks for r in members)
    cost_total = sum((r.total_cost_usd for r in members), Decimal(0))
    tokens_total = sum(r.total_tokens for r in members)

    counts = _sum_cost_sources(members, cost_by_run)
    return LeaderboardRow(
        rank=0,  # 排完序才知道，见 summarize
        agent_config_id=head.agent_config_id,
        label=head.label,
        agent_name=head.agent_name,
        agent_display_name=head.agent_display_name,
        agent_version=head.agent_version,
        model_name=head.model_name,
        protocol_version=head.protocol_version,
        run_count=len(members),
        run_ids=tuple(r.evaluation_run_id for r in members),
        tasks_per_run=head.total_tasks,
        resolve_rate_mean=_mean(rates),
        resolve_rate_min=min(rates) if rates else None,
        resolve_rate_max=max(rates) if rates else None,
        resolve_rate_spread=(max(rates) - min(rates)) if rates else None,
        effective_resolve_rate_mean=_mean(effective),
        resolved_mean=(
            Decimal(sum(r.resolved_count for r in members)) / Decimal(len(members))
        ).quantize(_RATE_PLACES),
        cost_usd_total=cost_total,
        cost_per_task=_cost_per_task(cost_total, tasks_total, counts),
        cost_lower_bound=counts[CostSource.UNAVAILABLE] > 0,
        cost_reported_attempts=counts[CostSource.REPORTED],
        cost_estimated_attempts=counts[CostSource.ESTIMATED],
        cost_unavailable_attempts=counts[CostSource.UNAVAILABLE],
        total_tokens=tokens_total,
        tokens_per_task=(tokens_total // tasks_total) if tasks_total else None,
        makespan_ms_mean=(sum(makespans) // len(makespans)) if makespans else None,
        infra_failure_total=sum(r.infra_failure_count for r in members),
        retry_total=sum(r.retry_count for r in members),
        facets=_merge_facets(members, facet_by_run),
    )


def _cost_per_task(
    cost_total: Decimal, tasks_total: int, counts: dict[CostSource, int]
) -> Decimal | None:
    """每题成本。**一次都报不出成本时返回 None；部分报不出时返回下界。**

    第一版的规则是"有任何一次报不出就 None"，挡的是这个错：claude-code 走中转
    端点时 44 次全报 `unavailable`（E3-T5 定的规矩），总额是 $0.00 —— 按
    "已知部分 / 全部题数"算出来是每题 $0，于是 `?metric=cost` 把它排在解决率
    13.6% 的 aider 前面，而 §18.6 第七节手算出来它是 **$0.042/题，比 aider 贵 2.4 倍**。
    这一档（全缺）现在仍然是 None。

    2026-09-21 放宽的是**部分缺**：E10-T4 两轮真实数据里，claude-code 84 次
    attempt 缺 2 次（两次 `AGENT_AUTH_ERROR` 重试，没花钱）、aider 缺 2 次
    （两次 `AGENT_TIMEOUT`，被平台掐掉、没来得及报），按第一版规则三个参赛者
    两个没有每题成本、散点图只剩一个点 —— 为了 2/84 把整列抹掉，比给一个
    标明"只会更高"的下界更误导。所以：只要有一次报得出，就返回
    `已知部分 / 全部题数`，并由 `cost_lower_bound` 告诉前端它是下界。

    "报不出成本 ≠ 最便宜"（§14.5 第五条）仍然成立：`_sort_key` 按成本排名时把
    下界行排在成本完整的行后面，不让它凭一个偏小的数上位。
    """
    unavailable = counts[CostSource.UNAVAILABLE]
    known = counts[CostSource.REPORTED] + counts[CostSource.ESTIMATED]
    if not tasks_total or (unavailable > 0 and known == 0):
        return None
    return (cost_total / Decimal(tasks_total)).quantize(_COST_PLACES)


def _sum_cost_sources(
    members: Sequence[EligibleRun], cost_by_run: dict[int, dict[CostSource | None, int]]
) -> dict[CostSource, int]:
    """把各轮的成本来源构成加起来。

    `cost_source` 为 NULL 的 attempt（还没跑完、或者被取消）不算进任何一档 ——
    它不是"报不出成本"，是"还没到报成本那一步"。
    """
    totals: dict[CostSource, int] = dict.fromkeys(CostSource, 0)
    for run in members:
        for source, attempts in cost_by_run.get(run.evaluation_run_id, {}).items():
            if source is not None:
                totals[source] += attempts
    return totals


def _merge_facets(
    members: Sequence[EligibleRun], facet_by_run: dict[int, list[FacetCount]]
) -> tuple[FacetCell, ...]:
    """把各轮的分面数字按取值加起来，按取值排序。

    跨轮相加而不是取平均：分面的格子本来就小（22 道题分到三个难度，
    一格可能只有 3 道），一轮一轮看噪声太大，合起来分母才够用。
    """
    merged: dict[str, list[int]] = {}
    for run in members:
        for cell in facet_by_run.get(run.evaluation_run_id, []):
            slot = merged.setdefault(cell.value, [0, 0])
            slot[0] += cell.resolved
            slot[1] += cell.total
    return tuple(
        FacetCell(
            value=value,
            resolved=resolved,
            total=total,
            resolve_rate=_rate(resolved, total),
        )
        for value, (resolved, total) in sorted(merged.items())
    )


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    """平均。一个都没有时是 None，不是 0 —— "没有数据"和"结果是零"不是一回事。"""
    if not values:
        return None
    return (sum(values, Decimal(0)) / Decimal(len(values))).quantize(_RATE_PLACES)


def _rate(numerator: int, denominator: int) -> Decimal | None:
    if denominator <= 0:
        return None
    return (Decimal(numerator) / Decimal(denominator)).quantize(_RATE_PLACES)


def _with_rank(row: LeaderboardRow, rank: int) -> LeaderboardRow:
    """名次是排完序才知道的，所以这里重建一份（行是 frozen 的）。

    用 `dataclasses.replace` 而不是 `**row.__dict__`：加了 `slots=True` 的
    dataclass 根本没有 `__dict__`。
    """
    return replace(row, rank=rank)


#: 排序时用来把"没有这个数"垫到最后的哨兵值。
_LAST = Decimal("999999999")


def _sort_key(metric: LeaderboardMetric):  # type: ignore[no-untyped-def]
    """排序键。**最后两项永远是 `(agent_config_id, protocol_version)`。**

    这不是好看，是 AC-4 要的稳定排序：两个参赛者解决率一模一样时，
    没有兜底键的话两次请求可能给出两个顺序，前端翻页就会重复或漏行。
    """

    def key(row: LeaderboardRow) -> tuple[object, ...]:
        rate = row.resolve_rate_mean if row.resolve_rate_mean is not None else Decimal(-1)
        cost = row.cost_per_task if row.cost_per_task is not None else _LAST
        duration = Decimal(row.makespan_ms_mean) if row.makespan_ms_mean is not None else _LAST
        tokens = Decimal(row.tokens_per_task) if row.tokens_per_task is not None else _LAST
        if metric is LeaderboardMetric.COST:
            # 成本完整的排前面，下界（有 attempt 报不出）的排后面，全缺的垫底：
            # "报不出成本 ≠ 最便宜"（§14.5 第五条）
            primary: tuple[object, ...] = (int(row.cost_lower_bound), cost, -rate)
        elif metric is LeaderboardMetric.DURATION:
            primary = (duration, -rate)
        elif metric is LeaderboardMetric.TOKENS:
            primary = (tokens, -rate)
        else:
            # 解决率相同就比每题成本：同样的成绩，便宜的排前面
            primary = (-rate, cost)
        return (*primary, row.agent_config_id, row.protocol_version)

    return key


def excluded_runs(
    session: Session, *, benchmark_set_id: int | None = None
) -> list[tuple[int, str]]:
    """被人工排除的实验和理由，按 id 升序。

    排行榜要把它连同榜单一起返回：**被排掉的东西不说出来，榜单就没法复核。**
    """
    stmt = (
        sa.select(EvaluationRun.id, EvaluationRun.leaderboard_excluded_reason)
        .where(EvaluationRun.leaderboard_excluded_reason.is_not(None))
        .order_by(EvaluationRun.id)
    )
    if benchmark_set_id is not None:
        stmt = stmt.where(EvaluationRun.benchmark_set_id == benchmark_set_id)
    return [(int(run_id), str(reason)) for run_id, reason in session.execute(stmt)]


__all__ = [
    "SENTINEL_KINDS",
    "CostSourceCount",
    "EligibleRun",
    "FacetCell",
    "FacetCount",
    "LeaderboardFacet",
    "LeaderboardMetric",
    "LeaderboardRow",
    "cost_sources",
    "eligible_runs",
    "excluded_runs",
    "facet_counts",
    "summarize",
]
