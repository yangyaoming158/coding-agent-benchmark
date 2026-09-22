"""失败分析接口（E7-T6）。

口径不在这里：类别分布、Agent × 类别热力图、Top 失败案例全由
`app.report.aggregate.failure_summary()` 算 —— 报告 JSON 的 `failures` 段就是它。
这一层只做两件事：决定看哪几次实验，以及盲检期间把逐案例的答案藏掉。

## 看哪几次实验

- `?set=<slug>&version=<v>`：这一版数据集上**所有排行榜准入**的实验。准入口径复用
  `app.analytics.leaderboard.eligible_runs()`，和榜单筛的是同一批 —— 榜上说 claude-code
  两轮 76.8%，这里的失败就是那两轮的失败，不多不少。
- `?run=158&run=167`：指定实验号，用来看被排除的、哨兵的、或者跨数据集的。

## 盲检

`BENCH_BLIND_REVIEW=true` 时逐案例的类别、状态、理由置空并标 `attribution_withheld`。
分布和热力图照常给：标注者从"F6 一共 30 条"猜不出自己手上这一条是什么。
"""

from __future__ import annotations

from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Query, status
from pydantic import BaseModel

from app.analytics import leaderboard as agg
from app.api.deps import SessionDep
from app.api.errors import READ_ERROR_RESPONSES, ApiError, not_found
from app.benchmark.dataset import DatasetError, resolve_set
from app.infrastructure.config import get_settings
from app.infrastructure.models.evaluation import EvaluationRun
from app.report.aggregate import failure_summary
from app.report.models import FailureSummary

router = APIRouter(prefix="/api/analysis", tags=["analysis"])

#: Top 案例最多给多少条。页面上一张表，再多就该去 /runs/[id] 的网格看。
MAX_TOP_N = 200


class AnalysisResponse(BaseModel):
    """失败分析 = 报告的 failures 段 + 它是按哪几次实验算的。"""

    #: `slug@version`；按实验号查时是 None。
    benchmark_set: str | None
    benchmark_set_id: int | None
    #: 实际参与统计的实验号（按数据集查时 = 该版的排行榜准入实验）。
    run_ids: list[int]
    #: 和报告 JSON `failures` 段同一个模型、同一个函数。
    failures: FailureSummary
    #: 为 true 时 `failures.top_cases` 里的类别 / 状态 / 理由是**被藏起来的**（盲检开关）。
    attribution_withheld: bool


@router.get("", response_model=AnalysisResponse, responses=READ_ERROR_RESPONSES)
def get_analysis(
    session: SessionDep,
    set_slug: Annotated[
        str | None,
        Query(alias="set", description="数据集 slug；取这一版上所有排行榜准入的实验"),
    ] = None,
    version: Annotated[str | None, Query(description="数据集版本，配合 set 用")] = None,
    run: Annotated[
        list[int] | None,
        Query(description="实验号，可重复给；和 set 二选一"),
    ] = None,
    top_n: Annotated[int, Query(ge=1, le=MAX_TOP_N, description="Top 失败案例条数")] = 50,
) -> AnalysisResponse:
    """失败分析：类别分布、Agent × 类别热力图、Top 失败案例。

    查询条数固定（数据集 1 + 准入 1 + 归因 4），不随实验数或题数增长（AC-7）。
    """
    if run and set_slug:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "ANALYSIS_SCOPE_AMBIGUOUS",
            "set 和 run 只能给一个",
        )
    benchmark_set: str | None = None
    benchmark_set_id: int | None = None
    if run:
        run_ids = _existing_runs(session, run)
    else:
        if set_slug is None:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "ANALYSIS_SCOPE_MISSING",
                "要给 set=<slug> 或至少一个 run=<id>",
            )
        try:
            dataset, _ = resolve_set(session, set_slug, version)
        except DatasetError as exc:
            raise not_found("BENCHMARK_SET_NOT_FOUND", str(exc)) from exc
        benchmark_set = f"{dataset.slug}@{dataset.version}"
        benchmark_set_id = dataset.id
        run_ids = [
            item.evaluation_run_id
            for item in agg.eligible_runs(session, benchmark_set_id=dataset.id)
        ]

    failures = failure_summary(session, run_ids, top_n=top_n)
    withheld = get_settings().blind_review
    if withheld:
        failures = failures.model_copy(
            update={
                "top_cases": [
                    case.model_copy(
                        update={"category": None, "attribution_status": None, "reasoning_zh": None}
                    )
                    for case in failures.top_cases
                ]
            }
        )
    return AnalysisResponse(
        benchmark_set=benchmark_set,
        benchmark_set_id=benchmark_set_id,
        run_ids=run_ids,
        failures=failures,
        attribution_withheld=withheld,
    )


def _existing_runs(session: SessionDep, run_ids: list[int]) -> list[int]:
    """按给的顺序去重，不存在的实验号直接 404 —— 静默跳过会让数字少一截还不报错。"""
    wanted = list(dict.fromkeys(run_ids))
    found = set(
        session.execute(sa.select(EvaluationRun.id).where(EvaluationRun.id.in_(wanted))).scalars()
    )
    missing = [run_id for run_id in wanted if run_id not in found]
    if missing:
        raise not_found(
            "RUN_NOT_FOUND", "找不到实验 " + ", ".join(f"#{run_id}" for run_id in missing)
        )
    return wanted


__all__ = ["MAX_TOP_N", "AnalysisResponse", "router"]
