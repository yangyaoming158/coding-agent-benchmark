"""数据集版本化与发布门禁（E1-T6，`03-benchmark-spec.md` §7.5 + 协议 C-50）。

这个模块回答三件事：**冻什么**、**凭什么发布**、**发布之后题目变了怎么办**。

## 一行 benchmark_sets 不等于"已发布"

`evaluation_runs.benchmark_set_id` 是非空外键，所以想跑 Oracle / Noop 门禁，
就得先有一行 `benchmark_sets`；而门禁的意思又是"不达标不许发布"。看着像死循环，
其实不是 —— **建行和发布是两回事**，发布只由 `status` 表示：

    stage    建一行 DRAFT，当场把题目清单 + content_hash 冻进 benchmark_set_items
    gate     建 Oracle / Noop 两个实验，题从 items 里取，快照摘要写进 run 的 manifest
    publish  重算摘要 → 要求两个门禁实验记的摘要与它一致 → 查门禁 → 全过才 PUBLISHED

这不是自欺欺人，关键在**门禁跑的题就是将要发布的那一批**：`stage` 那一刻就冻死了，
`publish` 不会"再查一次库里现在有哪些 VALID"。中间有人加题、改题、隔离题，
摘要就变了，旧门禁结果作废（`gate_verdict()` 的 `snapshot_mismatch`）。

## 为什么门禁判定放在 app.benchmark

`pyproject.toml` 的 import-linter 契约里，`app.evaluation | app.benchmark | app.report`
用的是 `|`，语义是**互不可见**。所以这里不能 import `app.evaluation.orchestrator`
或者 `app.evaluation.progress`。判定只读 `evaluation_runs` 已经落库的几个列和
`evaluation_task_runs` 的行，这些模型在 `app.infrastructure` 下，往下依赖是合法的。
三个命令的拼装放在 `cli/dataset.py` —— 和 `cli/promote.py` 一样，CLI 是组合层。

## 门禁为什么是三条而不是两条

协议 C-50 的字面要求是 Oracle 100% / Noop 0%，但只查这两个数会漏掉一整类问题：

**一道题因为平台故障没跑成，它同样不是 RESOLVED。** 于是 Noop 那边的"0%"可以被
凑出来 —— 门禁看着过了，其实那道题根本没验过。所以除了两个解决率，还要求
每道题都有一条 `infra_outcome = SUCCESS` 的认定结果。

Oracle 那一侧不存在这个漏洞（故障会让它掉出 100%），但一样查，
因为"哪几道题没跑成"是排查时第一个要知道的事。

## 已发布的版本一行都不改

§7.4 写"复验不通过的任务自动隔离并从当前 dataset 版本快照中排除（历史版本不受影响）"。
这句话如果理解成"从已发布版本的 items 里删行"，就和 §7.5 直接打架 ——
§7.5 存在的全部理由是"三周后重跑得到的是同一批题的同一版本"，删了行就不是了。
而且"历史版本不受影响"本身也讲不通：已发布的版本，发布那一刻起就是历史。

所以取唯一自洽的读法：**发布的版本永远不动，隔离影响的是下一版。**
题被置成 `QUARANTINED`，下一次 `stage` 自动不收它，v2 少一道题，v1 原样保留。
想知道手上这版 v1 里有没有已经被隔离的题，用 `drift()` 查出来 —— 报，但不改。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.benchmark.hashing import compute_content_hash, to_bare_hex
from app.domain.enums import (
    AgentOutcome,
    BenchmarkSetStatus,
    EvaluationRunStatus,
    InfraOutcome,
    TaskValidationState,
)
from app.domain.manifest import DATASET_SNAPSHOT_DIGEST_KEY
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.benchmark import (
    BenchmarkSet,
    BenchmarkSetItem,
    BenchmarkTask,
)
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun

#: 只有这个状态的题能进快照（§7.4：`VALID ──→ PUBLISHED（进入 dataset）`）。
#: `INVALID`（人工终审否掉的、八步验证挂掉的）、`REVIEW_REQUIRED`（还没人看）、
#: `QUARANTINED`（复验隔离的）一道都不收。
SNAPSHOT_STATE = TaskValidationState.VALID

#: 门禁用的两个哨兵 Agent 名字，和 `cli/seed.py` 里种下去的一致。
ORACLE_AGENT = "oracle"
NOOP_AGENT = "noop"

#: 版本号形状：`v` 加一个正整数，同一个 slug 下单调递增。
#:
#: 不用 `1.0` / `1.1` 这种两段式：那要求先定义清楚"什么改动算大版本"，
#: 而没人定义过。快照代数就是个计数器，用计数器的写法。
VERSION_PATTERN = re.compile(r"^v(\d+)$")

#: 快照摘要的前缀，和 `hashing.HASH_PREFIX` 一样，只是它算的是一批题不是一道题。
DIGEST_PREFIX = "sha256:"


class DatasetError(RuntimeError):
    """发布流程里"不该继续下去"的情形。CLI 接住它打印人话。"""


# ── 快照摘要 ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    """快照里的一行：一道题的身份 + 它当时的内容哈希。"""

    #: 库里的主键，投作业要用。
    benchmark_task_id: int
    #: `{owner}__{repo}-{pr}`，人看的那个 id，也是摘要的排序键。
    task_id: str
    #: 裸十六进制（64 位），和 `benchmark_tasks.content_hash` 一列同格式。
    content_hash: str


def snapshot_digest(rows: Sequence[SnapshotRow]) -> str:
    """一批题的聚合哈希，返回 `sha256:` 开头的字符串。

    算法：每行写成 `task_id:content_hash`，**按 task_id 排序**后用换行拼起来取 sha256。

    排序在这里是必须的，和 `hashing.canonical_json` 里"列表不排序"那条规矩不冲突：
    那条管的是一道题内部顺序有意义的字段，这里管的是一个**集合**——
    同样 22 道题，先入库谁后入库谁不该算成两个不同的数据集。

    摘要同时覆盖"有哪些题"和"每道题是什么内容"，所以它一个数就能回答
    "门禁跑的是不是将要发布的那一批"。
    """
    lines = sorted(f"{row.task_id}:{row.content_hash}" for row in rows)
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return f"{DIGEST_PREFIX}{digest}"


# ── 挑题 ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HashMismatch:
    """`content_hash` 那一列和 `raw_definition` 现算出来的对不上。"""

    task_id: str
    stored: str
    recomputed: str


@dataclass(frozen=True, slots=True)
class Selection:
    """按 `dataset_id` 挑出来的候选快照，还没写库。"""

    rows: tuple[SnapshotRow, ...]
    #: 重算 `content_hash` 对不上的题。**非空就不许冻快照**，理由见 `select_tasks()`。
    mismatched: tuple[HashMismatch, ...] = ()
    #: 属于这个 dataset 但状态不是 VALID 的题，按状态分类计数，报表要用。
    excluded: Mapping[str, int] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return snapshot_digest(self.rows)


def select_tasks(session: Session, dataset_id: str) -> Selection:
    """挑出某个 dataset 下全部 `VALID` 的题，顺便重算一遍 `content_hash`。

    **为什么要重算**：快照冻的是 `benchmark_tasks.content_hash` 那一列，而题目的
    内容在 `raw_definition` 里。两者对不上的时候我们不知道该信哪一份，
    此时冻下去的"身份证"是假的，`drift()` 以后报出来的漂移也全是噪声。
    这条和 §7.9 给 `test_patch_paths` 立的规矩是同一条：**导入与验证时重算，
    不一致则拒收**。

    重算很便宜：22 道题各一次 sha256，总共几毫秒。
    """
    stmt = (
        sa.select(
            BenchmarkTask.id,
            BenchmarkTask.task_id,
            BenchmarkTask.content_hash,
            BenchmarkTask.validation_state,
            BenchmarkTask.raw_definition,
        )
        .where(BenchmarkTask.raw_definition["dataset_id"].astext == dataset_id)
        .order_by(BenchmarkTask.task_id)
    )

    rows: list[SnapshotRow] = []
    mismatched: list[HashMismatch] = []
    excluded: dict[str, int] = {}

    for row_id, task_id, stored_hash, state, definition in session.execute(stmt):
        if state is not SNAPSHOT_STATE:
            excluded[state.value] = excluded.get(state.value, 0) + 1
            continue
        recomputed = to_bare_hex(compute_content_hash(definition or {}))
        if recomputed != stored_hash:
            mismatched.append(
                HashMismatch(task_id=task_id, stored=stored_hash, recomputed=recomputed)
            )
            continue
        rows.append(
            SnapshotRow(benchmark_task_id=row_id, task_id=task_id, content_hash=stored_hash)
        )

    return Selection(rows=tuple(rows), mismatched=tuple(mismatched), excluded=excluded)


# ── 版本号 ──────────────────────────────────────────────────


def next_version(existing: Sequence[str]) -> str:
    """下一个版本号。空的时候是 `v1`，否则是现有最大号加一。

    看不懂的版本号（比如手工填的 `1.0`）**不参与计算也不报错**：这个函数只保证
    新版本号不和现有的撞车，`uq_benchmark_sets_slug_version` 会兜住真正的冲突。
    """
    numbers = [int(m.group(1)) for v in existing if (m := VERSION_PATTERN.match(v))]
    return f"v{max(numbers) + 1 if numbers else 1}"


@dataclass(frozen=True, slots=True)
class StagePlan:
    """`stage` 这一次该干什么。三选一。"""

    #: `create` 新起一版、`refresh` 刷现有草稿、`noop` 什么都不用做。
    action: str
    #: `create` 时要用的版本号。
    version: str
    #: 已经有一版发布过、内容和这次一模一样时，它的版本号。
    same_as: str | None


def plan_stage(existing: Sequence[BenchmarkSet], digest_bare: str) -> StagePlan:
    """决定这次 `stage` 是新起一版、刷草稿，还是什么都不用做。**纯函数。**

    规则只有三条，按顺序：

    1. 最新的一版是 `DRAFT` → 刷它。草稿本来就是拿来改的。
    2. 否则，已经有一版发布过、内容和这次一模一样 → `noop`。
       **同一条命令跑两遍不该让版本号涨。** E8-T2 在 `assemble --limit` 上栽过
       同一类跟头：跑第二遍集合悄悄变大，比产生重复题更难发现。
    3. 否则新起一版。

    第 1 条排在第 2 条前面是有意的：草稿的内容碰巧和已发布版一样时，
    该刷的还是刷（并提醒一句"这一版没有新内容"），而不是拒绝动它 ——
    不然那份草稿会永远停在旧内容上。
    """
    versions = list(existing)
    same = next(
        (
            v
            for v in versions
            if v.status is BenchmarkSetStatus.PUBLISHED and v.snapshot_digest == digest_bare
        ),
        None,
    )
    latest = versions[0] if versions else None
    if latest is not None and latest.status is BenchmarkSetStatus.DRAFT:
        return StagePlan(
            action="refresh", version=latest.version, same_as=same.version if same else None
        )
    if same is not None:
        return StagePlan(action="noop", version=same.version, same_as=same.version)
    return StagePlan(
        action="create", version=next_version([v.version for v in versions]), same_as=None
    )


def versions_of(session: Session, slug: str) -> list[BenchmarkSet]:
    """某个 slug 的全部版本，新的在前。"""
    return list(
        session.execute(
            sa.select(BenchmarkSet)
            .where(BenchmarkSet.slug == slug)
            .order_by(BenchmarkSet.id.desc())
        ).scalars()
    )


def resolve_set(
    session: Session, slug: str, version: str | None = None, *, prefer_draft: bool = False
) -> tuple[BenchmarkSet, str | None]:
    """按 slug（可带版本号）取一个数据集版本，附带一句给人看的提醒。

    不给版本号时挑哪一版，**取决于调用方在干什么**，两种恰好相反：

    - **消费端**（`enqueue`、`experiment start`）默认 `prefer_draft=False`：
      先找最新的 `PUBLISHED`，没有再退回最新的、**有题的** `DRAFT` 并提醒一句。
      退回那一条是给开发期留的 —— 起个 Worker 冒烟一下不该被迫先走完发布流程 ——
      但它必须**说出来**，悄悄拿一版没过门禁的题去跑，结果会被当成正式数字。
    - **发布端**（`gate`、`publish`）传 `prefer_draft=True`：先找最新的、有题的 `DRAFT`。
      正在做的那一版才是要跑门禁、要发布的那一版。反过来的话，v1 发布之后
      `gate --slug X` 会解析到 v1 然后说"已经是 PUBLISHED，不用再跑门禁"，
      而人明明是想给刚 stage 出来的 v2 跑门禁。
    """
    versions = versions_of(session, slug)
    if not versions:
        raise DatasetError(f"找不到数据集 {slug}，先跑 `python -m cli.dataset stage`")

    if version is not None:
        for candidate in versions:
            if candidate.version == version:
                return candidate, None
        have = "、".join(v.version for v in versions)
        raise DatasetError(f"{slug} 没有 {version} 这一版，现有：{have}")

    def first_draft() -> BenchmarkSet | None:
        return next(
            (v for v in versions if v.status is BenchmarkSetStatus.DRAFT and v.task_count > 0),
            None,
        )

    def first_published() -> BenchmarkSet | None:
        return next((v for v in versions if v.status is BenchmarkSetStatus.PUBLISHED), None)

    if prefer_draft:
        draft = first_draft()
        if draft is not None:
            return draft, None
        published = first_published()
        if published is not None:
            return published, None
    else:
        published = first_published()
        if published is not None:
            return published, None
        draft = first_draft()
        if draft is not None:
            note = f"{slug} 还没有已发布的版本，用的是草稿 {draft.version}（没过门禁）"
            return draft, note

    raise DatasetError(
        f"{slug} 没有一个版本是有题的，先跑 `python -m cli.dataset stage --dataset-id <id>`"
    )


# ── 冻快照 ──────────────────────────────────────────────────


def freeze(session: Session, dataset: BenchmarkSet, rows: Sequence[SnapshotRow]) -> None:
    """把题目清单冻进 `benchmark_set_items`，并更新 set 上的三个派生字段。

    **只能对 DRAFT 干这件事。** 已发布的版本一行都不改（模块文档最后一节），
    这里直接抛异常而不是静默跳过 —— 静默跳过的话，调用方以为自己更新了快照，
    实际什么都没发生，而这种错要等到下次复现实验对不上才被发现。

    `position` 按 `task_id` 排序写死，让"第几道题"是个确定的事实。
    """
    if dataset.status is not BenchmarkSetStatus.DRAFT:
        raise DatasetError(
            f"{dataset.slug}@{dataset.version} 的状态是 {dataset.status.value}，"
            "已发布的版本不许改题目清单；要改就出新版本"
        )

    session.execute(
        sa.delete(BenchmarkSetItem).where(BenchmarkSetItem.benchmark_set_id == dataset.id)
    )
    ordered = sorted(rows, key=lambda r: r.task_id)
    for position, row in enumerate(ordered, start=1):
        session.add(
            BenchmarkSetItem(
                benchmark_set_id=dataset.id,
                benchmark_task_id=row.benchmark_task_id,
                task_content_hash=row.content_hash,
                position=position,
            )
        )
    dataset.task_count = len(ordered)
    dataset.snapshot_digest = to_bare_hex(snapshot_digest(ordered))
    session.flush()


def items_of(session: Session, benchmark_set_id: int) -> list[SnapshotRow]:
    """读回一个版本冻住的题目清单，按 `position` 排。"""
    stmt = (
        sa.select(
            BenchmarkSetItem.benchmark_task_id,
            BenchmarkTask.task_id,
            BenchmarkSetItem.task_content_hash,
        )
        .join(BenchmarkTask, BenchmarkTask.id == BenchmarkSetItem.benchmark_task_id)
        .where(BenchmarkSetItem.benchmark_set_id == benchmark_set_id)
        .order_by(BenchmarkSetItem.position)
    )
    return [
        SnapshotRow(benchmark_task_id=task_row_id, task_id=task_id, content_hash=content_hash)
        for task_row_id, task_id, content_hash in session.execute(stmt)
    ]


def current_digest(session: Session, benchmark_set_id: int) -> str:
    """现算一遍某个版本的快照摘要（带 `sha256:` 前缀）。"""
    return snapshot_digest(items_of(session, benchmark_set_id))


# ── 门禁 ────────────────────────────────────────────────────

#: 门禁实验的 `manifest` 里记快照摘要用的键。
#:
#: `evaluation_runs.manifest` 这一列的注释原文就写着它装"镜像 digest 表、
#: harness 的 git sha、**数据集哈希**"，所以不用新开表。E5-T4 往同一个 JSONB 里
#: 补了别的键，这一个位置和名字都没动 —— `gate_verdict()` 靠这条 JSONB 路径找
#: 门禁实验，而已发布版本的门禁记录不许事后修改。
#:
#: 名字的定义搬去了 `app.domain.manifest`：`app.evaluation` 建实验时也要写这个键，
#: 而 import-linter 的契约里 `app.evaluation | app.benchmark` 互不可见，
#: 两边各写一个字面量的话，改名时漏掉的那一边**不会报错**，只是查不到门禁记录。
MANIFEST_DIGEST_KEY = DATASET_SNAPSHOT_DIGEST_KEY


@dataclass(frozen=True, slots=True)
class SentinelResult:
    """一个哨兵实验跑出来的、门禁关心的那几个数。"""

    run_id: int
    agent_name: str
    status: EvaluationRunStatus
    total_tasks: int
    #: 有认定结果（canonical attempt）的题数。
    completed_tasks: int
    resolved_count: int
    #: 认定结果的 `infra_outcome` 不是 SUCCESS 的题，以及压根没有认定结果的题。
    not_clean: tuple[str, ...]
    #: 判成 RESOLVED 的题的 task_id，报错时要指名道姓。
    resolved_tasks: tuple[str, ...]
    #: 没判成 RESOLVED 的题的 task_id。
    unresolved_tasks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """门禁的结论。`ok` 为真才允许发布。"""

    ok: bool
    #: 不放行的理由，一条一句人话。`ok` 为真时是空的。
    problems: tuple[str, ...]
    oracle: SentinelResult | None
    noop: SentinelResult | None
    #: 快照摘要（带前缀），发布证据里要记。
    digest: str


def load_sentinel(
    session: Session, *, benchmark_set_id: int, agent_name: str, digest: str
) -> SentinelResult | None:
    """找针对**这一份快照**跑的、最新的一次哨兵实验。

    三个条件同时满足才算数：属于这个 set、用的是这个哨兵 Agent、
    `manifest` 里记的快照摘要和现在一致。第三条是门禁不能被绕过的关键 ——
    快照在门禁跑完之后被改过，摘要就变了，这次实验查不出来，等于没跑。

    同样条件有多次时取 id 最大的那次：门禁失败之后修完题再跑一遍是正常操作，
    该看最新那次的结论。
    """
    run = session.execute(
        sa.select(EvaluationRun)
        .join(AgentConfig, AgentConfig.id == EvaluationRun.agent_config_id)
        .join(Agent, Agent.id == AgentConfig.agent_id)
        .where(
            EvaluationRun.benchmark_set_id == benchmark_set_id,
            Agent.name == agent_name,
            EvaluationRun.manifest[MANIFEST_DIGEST_KEY].astext == digest,
        )
        .order_by(EvaluationRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if run is None:
        return None

    rows = session.execute(
        sa.select(
            BenchmarkTask.task_id,
            EvaluationTaskRun.infra_outcome,
            EvaluationTaskRun.agent_outcome,
        )
        .join(BenchmarkTask, BenchmarkTask.id == EvaluationTaskRun.benchmark_task_id)
        .where(
            EvaluationTaskRun.evaluation_run_id == run.id,
            EvaluationTaskRun.is_canonical.is_(True),
        )
        .order_by(BenchmarkTask.task_id)
    ).all()

    seen = {task_id for task_id, _, _ in rows}
    not_clean = [task_id for task_id, infra, _ in rows if infra is not InfraOutcome.SUCCESS]
    # 快照里有、但一条认定结果都没有的题，同样算"没验干净"：作业死在队列里、
    # 实验被取消都会走到这里，而它们和"跑出了平台故障"要报的话是一样的。
    not_clean.extend(
        row.task_id for row in items_of(session, benchmark_set_id) if row.task_id not in seen
    )
    resolved = [t for t, _, agent in rows if agent is AgentOutcome.RESOLVED]
    unresolved = [t for t, _, agent in rows if agent is not AgentOutcome.RESOLVED]

    return SentinelResult(
        run_id=run.id,
        agent_name=agent_name,
        status=run.status,
        total_tasks=run.total_tasks,
        completed_tasks=len(rows),
        resolved_count=len(resolved),
        not_clean=tuple(sorted(not_clean)),
        resolved_tasks=tuple(resolved),
        unresolved_tasks=tuple(unresolved),
    )


def check_sentinel(
    result: SentinelResult | None, *, agent_name: str, expected_tasks: int, want_resolved: int
) -> list[str]:
    """一个哨兵实验的四条检查，返回不合格的理由（空列表 = 合格）。

    **纯函数**，不碰数据库 —— 门禁的判断规则要能脱开一整套评测链路单独测。
    四条分别是：跑完了没、跑的题数对不对、每道题的结果干不干净、解决率对不对。
    """
    label = f"{agent_name} 哨兵"
    if result is None:
        return [f"{label}还没跑（或者跑的是另一份快照），先跑 `dataset gate`"]

    problems: list[str] = []
    if result.status is not EvaluationRunStatus.COMPLETED:
        problems.append(
            f"{label}（实验 #{result.run_id}）的状态是 {result.status.value}，不是 COMPLETED"
        )
    if result.total_tasks != expected_tasks:
        problems.append(
            f"{label}（实验 #{result.run_id}）跑了 {result.total_tasks} 道题，"
            f"快照里有 {expected_tasks} 道"
        )
    if result.not_clean:
        problems.append(
            f"{label}有 {len(result.not_clean)} 道题没拿到干净的结果："
            f"{_join_task_ids(result.not_clean)}"
        )
    if result.resolved_count != want_resolved:
        rate = result.resolved_count / result.total_tasks if result.total_tasks else 0.0
        offenders = result.unresolved_tasks if want_resolved else result.resolved_tasks
        problems.append(
            f"{label}解决率 {rate:.1%}（{result.resolved_count}/{result.total_tasks}），"
            f"要求 {want_resolved}/{expected_tasks}；出问题的题：{_join_task_ids(offenders)}"
        )
    return problems


def _join_task_ids(task_ids: Sequence[str], limit: int = 8) -> str:
    """把 task_id 列表打印成一行，太长就截断。门禁失败时要指名道姓，不能只报个数字。"""
    if not task_ids:
        return "（无）"
    head = list(task_ids[:limit])
    tail = f" 等 {len(task_ids)} 道" if len(task_ids) > limit else ""
    return "、".join(head) + tail


def gate_verdict(session: Session, dataset: BenchmarkSet) -> GateVerdict:
    """跑一遍发布门禁（协议 C-50），返回能不能发布。

    **只读，不改任何东西。** 允许在发布前反复问"现在能发了吗"。
    """
    rows = items_of(session, dataset.id)
    digest = snapshot_digest(rows)
    problems: list[str] = []

    if not rows:
        return GateVerdict(
            ok=False,
            problems=("快照是空的，先跑 `dataset stage`",),
            oracle=None,
            noop=None,
            digest=digest,
        )

    # 存的摘要和现算的对不上 = 有人绕过发布流程直接改了 items
    stored = dataset.snapshot_digest
    if stored is not None and stored != to_bare_hex(digest):
        problems.append(
            f"快照摘要对不上：库里存的是 {stored[:12]}…，现算出来是 {to_bare_hex(digest)[:12]}…；"
            "题目清单被绕过发布流程改过，重跑 `dataset stage`"
        )

    oracle = load_sentinel(
        session, benchmark_set_id=dataset.id, agent_name=ORACLE_AGENT, digest=digest
    )
    noop = load_sentinel(session, benchmark_set_id=dataset.id, agent_name=NOOP_AGENT, digest=digest)
    problems += check_sentinel(
        oracle, agent_name=ORACLE_AGENT, expected_tasks=len(rows), want_resolved=len(rows)
    )
    problems += check_sentinel(
        noop, agent_name=NOOP_AGENT, expected_tasks=len(rows), want_resolved=0
    )

    return GateVerdict(
        ok=not problems, problems=tuple(problems), oracle=oracle, noop=noop, digest=digest
    )


def evidence_of(verdict: GateVerdict) -> dict[str, Any]:
    """把门禁结论压成一个能塞进 `benchmark_sets.publish_evidence` 的字典。"""
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "snapshot_digest": verdict.digest,
        "protocol_clause": "C-50",
        "oracle": _sentinel_evidence(verdict.oracle),
        "noop": _sentinel_evidence(verdict.noop),
    }


def _sentinel_evidence(result: SentinelResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "evaluation_run_id": result.run_id,
        "total_tasks": result.total_tasks,
        "resolved_count": result.resolved_count,
        "resolve_rate": (
            round(result.resolved_count / result.total_tasks, 4) if result.total_tasks else None
        ),
    }


# ── 漂移检查 ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Drift:
    """一个已发布版本和现在的题库之间的差异。三类，处置完全不同。"""

    #: 题还在，但 `content_hash` 变了 —— 题目被改过，这一版不再等于现在的库。
    changed: tuple[str, ...]
    #: 题被隔离了（`QUARANTINED`）—— 已发布版本不动，下一版会自动排除它。
    quarantined: tuple[str, ...]
    #: 题在 `benchmark_tasks` 里找不到了。外键是 RESTRICT，正常删不掉，
    #: 真出现说明有人绕过 ORM 动了库。
    missing: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not (self.changed or self.quarantined or self.missing)


def drift(session: Session, benchmark_set_id: int) -> Drift:
    """拿一个版本的快照逐题比对现在的题库。**纯查询，不起容器、不改任何东西。**

    这是 NFR-02（可复现性）的可验证形式：三周后想重跑某一版，先跑这个，
    就知道"我手上这份数据集是不是当初那一份"。
    """
    stmt = (
        sa.select(
            BenchmarkSetItem.task_content_hash,
            BenchmarkTask.task_id,
            BenchmarkTask.content_hash,
            BenchmarkTask.validation_state,
        )
        .outerjoin(BenchmarkTask, BenchmarkTask.id == BenchmarkSetItem.benchmark_task_id)
        .where(BenchmarkSetItem.benchmark_set_id == benchmark_set_id)
        .order_by(BenchmarkSetItem.position)
    )
    changed: list[str] = []
    quarantined: list[str] = []
    missing: list[str] = []
    for frozen_hash, task_id, live_hash, state in session.execute(stmt):
        if task_id is None:
            missing.append(f"benchmark_task_id 已不存在（冻的哈希 {frozen_hash[:12]}…）")
            continue
        if live_hash != frozen_hash:
            changed.append(task_id)
        if state is TaskValidationState.QUARANTINED:
            quarantined.append(task_id)
    return Drift(changed=tuple(changed), quarantined=tuple(quarantined), missing=tuple(missing))


# ── 发布产物 ────────────────────────────────────────────────


def build_manifest(
    dataset: BenchmarkSet,
    rows: Sequence[SnapshotRow],
    *,
    dataset_sha256: str,
    harness_git_sha: str,
    dirty: bool,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """指纹文件的内容，字段按 `12-engineering-workflow.md` §32.6。

    §32.6 的分层是：DB 里的 `benchmark_set_items` 是事实来源，导出的 jsonl 是完整内容
    （太大不入库），Git 里存的这份 **只是指纹**。任何人拿到仓库加制品，
    就能校验"我手上这份数据集是不是当初那一份"。

    `task_hashes_sha256` 是快照摘要（题目清单的哈希），`dataset_sha256` 是导出文件
    本身的哈希。两个都要：前者从库里就能重算，后者证明手上那个 jsonl 没被动过。

    `tasks` 那一列**在这里排序**，不指望调用方传进来就是有序的。指纹文件是要入库的，
    同一份快照生成两次必须逐字节相同，否则每发布一次 git 就多一条无意义的 diff。
    """
    ordered = sorted(rows, key=lambda r: r.task_id)
    return {
        "slug": dataset.slug,
        "version": dataset.version,
        "source_dataset_id": dataset.source_dataset_id,
        "task_count": len(ordered),
        "dataset_sha256": dataset_sha256,
        "task_hashes_sha256": snapshot_digest(ordered),
        "published_at": (
            dataset.published_at.isoformat() if dataset.published_at is not None else None
        ),
        "harness_git_sha": harness_git_sha,
        # 协议 C-28：工作区不干净时跑出来的结果不得进排行榜。门禁实验是"凭它发布
        # 数据集"的记录，恒为 false 的 dirty 比不记更糟，所以如实写。
        "dirty": dirty,
        "gate": dict(evidence),
        "tasks": [{"task_id": r.task_id, "content_hash": r.content_hash} for r in ordered],
    }


def export_lines(session: Session, rows: Sequence[SnapshotRow]) -> list[str]:
    """把快照里的题目导成 jsonl 的每一行（`raw_definition` 原样）。

    **这个文件不入库**：它含 `gold_patch`，协议 C-44 要求官方补丁永不下发给被测 AI，
    而 `.gitignore` 里 `datasets/exports/` 已经挡住了。

    行内的键排序，行间按 `task_id` 排序 —— 同一份快照导出两次必须逐字节相同，
    否则 `dataset_sha256` 每导一次变一个值，指纹就没有意义了。
    """
    ordered = sorted(rows, key=lambda r: r.task_id)
    fetched = session.execute(
        sa.select(BenchmarkTask.id, BenchmarkTask.raw_definition).where(
            BenchmarkTask.id.in_([r.benchmark_task_id for r in ordered])
        )
    ).all()
    by_id: dict[int, dict[str, object]] = dict(fetched)  # type: ignore[arg-type]
    return [
        json.dumps(by_id[row.benchmark_task_id], sort_keys=True, ensure_ascii=False)
        for row in ordered
    ]


def sha256_of(text: str) -> str:
    """一段文本的 sha256，带 `sha256:` 前缀。"""
    return f"{DIGEST_PREFIX}{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


__all__ = [
    "DIGEST_PREFIX",
    "MANIFEST_DIGEST_KEY",
    "NOOP_AGENT",
    "ORACLE_AGENT",
    "SNAPSHOT_STATE",
    "DatasetError",
    "Drift",
    "GateVerdict",
    "HashMismatch",
    "Selection",
    "SentinelResult",
    "SnapshotRow",
    "StagePlan",
    "build_manifest",
    "check_sentinel",
    "current_digest",
    "drift",
    "evidence_of",
    "export_lines",
    "freeze",
    "gate_verdict",
    "items_of",
    "load_sentinel",
    "next_version",
    "plan_stage",
    "resolve_set",
    "select_tasks",
    "sha256_of",
    "snapshot_digest",
    "versions_of",
]
