"""排行榜接口（E7-T0）。

聚合口径不在这里，在 `app.evaluation.leaderboard` —— 这一层只做参数解析和响应拼装。
"谁有资格上榜"是业务规则，业务规则要能不起 HTTP 单测（和 AC-2 对写操作的要求同一条理由）。

## 榜单必须能自证

响应里除了排名，还有两样东西：

- `eligibility`：这次筛掉了什么，六条规则各是什么。
- `excluded_runs`：被人工排除的实验和理由。

**一个不说自己筛掉了什么的排行榜没法复核。** 库里现在就有 4 个实验
（#119–#122）符合协议的全部准入条件、数字却完全不是测量结果 ——
如果榜单只给一串名次，谁都看不出它们被处理过。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import Page, PageDep, SessionDep, page_of
from app.api.errors import READ_ERROR_RESPONSES, not_found
from app.benchmark.dataset import DatasetError, resolve_set
from app.evaluation import leaderboard as agg
from app.infrastructure.models.benchmark import BenchmarkSet
from app.infrastructure.models.evaluation import EvaluationRun

router = APIRouter(prefix="/api/leaderboard", tags=["leaderboard"])


class FacetCellOut(BaseModel):
    """某个分面取值上的成绩。`value` 是枚举原值，不做中文映射（AC-10）。"""

    value: str
    resolved: int
    total: int
    resolve_rate: Decimal | None


class LeaderboardRowOut(BaseModel):
    """榜单的一行 = 一个参赛者 × 一个协议版本。"""

    rank: int
    agent_config_id: int
    label: str
    agent_name: str
    agent_display_name: str
    agent_version: str
    model_name: str
    #: 协议 C-59：不同协议版本的结果不混排，所以它是分组键的一部分。
    protocol_version: str

    run_count: int
    #: 具体是哪几次实验。前端画散点、追证据都要它。
    run_ids: list[int]
    tasks_per_run: int

    #: 严格解决率（协议 C-21：RESOLVED 题数 / 题库总题数）的轮间平均。
    resolve_rate_mean: Decimal | None
    resolve_rate_min: Decimal | None
    resolve_rate_max: Decimal | None
    #: 最大减最小。实测同一批题跑两遍能差 9 个百分点（§18.6 第六节），
    #: 只看平均会误导，所以这个数必须显示出来。
    resolve_rate_spread: Decimal | None
    effective_resolve_rate_mean: Decimal | None
    resolved_mean: Decimal

    cost_usd_total: Decimal
    cost_per_task: Decimal | None
    #: 成本来源构成（协议纪律 3）。`cost_unavailable_attempts` 大于 0 时，
    #: 上面那个金额是**偏低的** —— 前端要显示"成本不可用"而不是显示 0。
    cost_reported_attempts: int
    cost_estimated_attempts: int
    cost_unavailable_attempts: int

    total_tokens: int
    tokens_per_task: int | None
    makespan_ms_mean: int | None
    infra_failure_total: int
    retry_total: int

    #: 请求了 `facet` 才有。
    facets: list[FacetCellOut]


class ExcludedRun(BaseModel):
    """被人工排除出排行榜的一次实验。"""

    evaluation_run_id: int
    reason: str


class LeaderboardResponse(BaseModel):
    """榜单 + 它是怎么筛出来的。"""

    benchmark_set_id: int
    benchmark_set: str
    metric: agg.LeaderboardMetric
    facet: agg.LeaderboardFacet | None
    rows: Page[LeaderboardRowOut]
    #: 六条准入规则的人话版本，给前端直接显示在榜单下面。
    eligibility: list[str]
    #: 被人工排除的实验，连同理由。
    excluded_runs: list[ExcludedRun]


#: 六条准入规则。和 `app.evaluation.leaderboard.eligible_runs()` 里的
#: WHERE 一一对应 —— 那边改了这边要跟着改，所以两处挨着写在同一个模块文档里。
ELIGIBILITY_RULES = [
    "实验状态是 COMPLETED（协议 C-26b：平台故障率超过 5% 的记 PARTIAL，不准入）",
    "dirty = false（协议 C-28：工作区不干净时跑的结果不得进排行榜）",
    "没有被人工排除（leaderboard_excluded_reason 为空）",
    "参赛者是启用状态（agent_configs.enabled）",
    "不是哨兵（Oracle / Noop / Mock 是量具，不是选手）",
    "跑满了整份数据集快照（严格解决率的分母是题库总题数，只跑几道题的不可比）",
]


@router.get("", response_model=LeaderboardResponse, responses=READ_ERROR_RESPONSES)
def get_leaderboard(
    session: SessionDep,
    page: PageDep,
    set_slug: Annotated[
        str | None,
        Query(alias="set", description="数据集 slug。不给就取最新已发布的那一版"),
    ] = None,
    version: Annotated[str | None, Query(description="数据集版本，配合 set 用")] = None,
    metric: Annotated[
        agg.LeaderboardMetric, Query(description="按什么排")
    ] = agg.LeaderboardMetric.RESOLVE_RATE,
    facet: Annotated[agg.LeaderboardFacet | None, Query(description="按难度/语言/仓库分面")] = None,
) -> LeaderboardResponse:
    """排行榜。

    **一个数据集版本一张榜。** 不给 `set` 就取最新已发布的那一版 ——
    不同数据集的解决率之间没有可比性，混在一起等于把两场考试的分数排在一起。

    查询条数固定：合格实验 1 条、成本来源 1 条、分面 0 或 1 条（AC-7）。
    """
    try:
        dataset, _ = resolve_set(session, set_slug or _default_slug(session), version)
    except DatasetError as exc:
        raise not_found("BENCHMARK_SET_NOT_FOUND", str(exc)) from exc

    runs = agg.eligible_runs(session, benchmark_set_id=dataset.id)
    run_ids = [run.evaluation_run_id for run in runs]
    rows = agg.summarize(
        runs,
        costs=agg.cost_sources(session, run_ids),
        facets=agg.facet_counts(session, run_ids, facet=facet) if facet else (),
        metric=metric,
    )
    window = rows[page.offset : page.offset + page.limit]
    return LeaderboardResponse(
        benchmark_set_id=dataset.id,
        benchmark_set=f"{dataset.slug}@{dataset.version}",
        metric=metric,
        facet=facet,
        rows=page_of([_row_out(row) for row in window], total=len(rows), params=page),
        eligibility=ELIGIBILITY_RULES,
        excluded_runs=[
            ExcludedRun(evaluation_run_id=run_id, reason=reason)
            for run_id, reason in agg.excluded_runs(session, benchmark_set_id=dataset.id)
        ],
    )


def _default_slug(session: Session) -> str:
    """不给 `set` 时用哪个数据集。

    取**有实验跑过的那个**里最新的一个 slug。硬编码一个名字（比如 `benchmark-dev`）
    会在换数据集时变成一个没人记得改的常量。
    """
    slug = session.execute(
        sa.select(BenchmarkSet.slug)
        .join(EvaluationRun, EvaluationRun.benchmark_set_id == BenchmarkSet.id)
        .order_by(EvaluationRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if slug is None:
        raise not_found(
            "BENCHMARK_SET_NOT_FOUND",
            "还没有任何实验跑在任何数据集上，排行榜是空的。"
            "指定一个数据集：/api/leaderboard?set=<slug>",
        )
    return str(slug)


def _row_out(row: agg.LeaderboardRow) -> LeaderboardRowOut:
    return LeaderboardRowOut(
        rank=row.rank,
        agent_config_id=row.agent_config_id,
        label=row.label,
        agent_name=row.agent_name,
        agent_display_name=row.agent_display_name,
        agent_version=row.agent_version,
        model_name=row.model_name,
        protocol_version=row.protocol_version,
        run_count=row.run_count,
        run_ids=list(row.run_ids),
        tasks_per_run=row.tasks_per_run,
        resolve_rate_mean=row.resolve_rate_mean,
        resolve_rate_min=row.resolve_rate_min,
        resolve_rate_max=row.resolve_rate_max,
        resolve_rate_spread=row.resolve_rate_spread,
        effective_resolve_rate_mean=row.effective_resolve_rate_mean,
        resolved_mean=row.resolved_mean,
        cost_usd_total=row.cost_usd_total,
        cost_per_task=row.cost_per_task,
        cost_reported_attempts=row.cost_reported_attempts,
        cost_estimated_attempts=row.cost_estimated_attempts,
        cost_unavailable_attempts=row.cost_unavailable_attempts,
        total_tokens=row.total_tokens,
        tokens_per_task=row.tokens_per_task,
        makespan_ms_mean=row.makespan_ms_mean,
        infra_failure_total=row.infra_failure_total,
        retry_total=row.retry_total,
        facets=[
            FacetCellOut(
                value=cell.value,
                resolved=cell.resolved,
                total=cell.total,
                resolve_rate=cell.resolve_rate,
            )
            for cell in row.facets
        ],
    )


__all__ = [
    "ELIGIBILITY_RULES",
    "ExcludedRun",
    "FacetCellOut",
    "LeaderboardResponse",
    "LeaderboardRowOut",
    "router",
]
