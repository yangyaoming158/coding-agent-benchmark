"""数据集版本的接口：列表和详情（E7-T0）。

对着 §16.2 的两个 P0 页面写的：

- **Benchmarks** `/benchmarks` 要"版本、题量、语言分布、来源构成" ——
  列表给前两个，详情的 `composition` 给后两个。
- **Benchmark Detail** `/benchmarks/[slug]` 要"验证证据、Oracle/Noop 自检结果" ——
  详情的 `publish_evidence` 给。

"语言分布"和"来源构成"没有单开端点，放在详情的 `composition` 里。
§14.4 原本有个 `GET /api/repositories`，但 P0 页面要的其实是
"**这一版数据集**里的题来自哪些仓库"，不是"库里有哪些仓库" ——
后者在前端没有任何落点。按 AC-1 那句"按前端 P0 页面倒推"，就不单开了。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import Page, PageDep, SessionDep, page_of
from app.api.errors import READ_ERROR_RESPONSES, not_found
from app.benchmark.dataset import DatasetError, resolve_set
from app.domain.enums import BenchmarkSetStatus
from app.infrastructure.models.benchmark import (
    BenchmarkSet,
    BenchmarkSetItem,
    BenchmarkTask,
    Repository,
)

router = APIRouter(prefix="/api/benchmark-sets", tags=["benchmark-sets"])


class BenchmarkSetSummary(BaseModel):
    """列表里的一行。"""

    id: int
    slug: str
    version: str
    title: str
    description: str | None
    #: 枚举原样透出，不做中文映射（AC-10）。`DRAFT` 不等于"已发布"。
    status: BenchmarkSetStatus
    task_count: int
    source_dataset_id: str | None
    #: `benchmark_set_items` 的聚合哈希。和现算的对不上说明有人绕过发布流程改了快照。
    snapshot_digest: str | None
    published_at: datetime | None
    created_at: datetime


class CompositionCell(BaseModel):
    """构成里的一格：某个取值下有几道题。"""

    value: str
    count: int


class BenchmarkSetDetail(BenchmarkSetSummary):
    """详情。比列表多两块：构成和发布证据。"""

    #: 按语言 / 难度 / 仓库的题目构成。前端的"语言分布""来源构成"直接用它。
    composition: dict[str, list[CompositionCell]]
    #: Oracle / Noop 两次门禁实验的 id 和解决率（协议 C-50）。
    #: 没跑过门禁的 DRAFT 是 None —— 如实返回 None，不编一个空对象。
    publish_evidence: dict[str, Any] | None


@router.get("", responses=READ_ERROR_RESPONSES)
def list_benchmark_sets(
    session: SessionDep,
    page: PageDep,
    slug: Annotated[str | None, Query(description="只看这个 slug 的版本")] = None,
    status: Annotated[BenchmarkSetStatus | None, Query(description="只看这个状态")] = None,
) -> Page[BenchmarkSetSummary]:
    """数据集版本列表，新的在前。"""
    where = []
    if slug is not None:
        where.append(BenchmarkSet.slug == slug)
    if status is not None:
        where.append(BenchmarkSet.status == status)

    total = int(
        session.execute(
            sa.select(sa.func.count()).select_from(BenchmarkSet).where(*where)
        ).scalar_one()
    )
    rows = (
        session.execute(
            sa.select(BenchmarkSet)
            .where(*where)
            # id 唯一，所以这个顺序是稳定的（AC-4）
            .order_by(BenchmarkSet.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
        .scalars()
        .all()
    )
    return page_of(
        [BenchmarkSetSummary.model_validate(row, from_attributes=True) for row in rows],
        total=total,
        params=page,
    )


@router.get("/{slug}", response_model=BenchmarkSetDetail, responses=READ_ERROR_RESPONSES)
def get_benchmark_set(
    slug: str,
    session: SessionDep,
    version: Annotated[str | None, Query(description="不给就取最新已发布的那一版")] = None,
) -> BenchmarkSetDetail:
    """一个数据集版本的详情。

    不给 `version` 时走和 `cli.experiment start` 同一个解析规则
    （`app.benchmark.dataset.resolve_set`）：先找最新的 `PUBLISHED`，
    没有再退回最新的、有题的 `DRAFT`。两边用同一个函数，
    免得"网页上看到的那一版"和"实际跑的那一版"是两个东西。
    """
    try:
        dataset, _ = resolve_set(session, slug, version)
    except DatasetError as exc:
        raise not_found("BENCHMARK_SET_NOT_FOUND", str(exc)) from exc

    summary = BenchmarkSetSummary.model_validate(dataset, from_attributes=True)
    return BenchmarkSetDetail(
        **summary.model_dump(),
        composition=composition_of(session, dataset.id),
        publish_evidence=dataset.publish_evidence,
    )


def composition_of(session: Session, benchmark_set_id: int) -> dict[str, list[CompositionCell]]:
    """这一版数据集的题目按语言 / 难度 / 仓库怎么分布。

    三条固定的 group by，**条数不随题数增长**（AC-7）。
    数的是冻进快照的题（`benchmark_set_items`），不是整张 `benchmark_tasks` ——
    那张表里混着别的数据集的题和 INVALID 的题。
    """

    def counts(column: sa.Cast[str], extra_join: Any = None) -> list[CompositionCell]:
        stmt = (
            sa.select(column, sa.func.count())
            .select_from(BenchmarkSetItem)
            .join(BenchmarkTask, BenchmarkTask.id == BenchmarkSetItem.benchmark_task_id)
        )
        if extra_join is not None:
            stmt = stmt.join(extra_join[0], extra_join[1])
        stmt = (
            stmt.where(BenchmarkSetItem.benchmark_set_id == benchmark_set_id)
            .group_by(column)
            .order_by(column)
        )
        return [
            CompositionCell(value=str(value), count=int(count))
            for value, count in session.execute(stmt)
        ]

    # 三个都 cast 成文本：枚举不 cast 的话 group by 出来的是枚举对象，
    # 而构成里的取值要原样透出成字符串（AC-10）。仓库名 cast 是个空操作，
    # 三条写法一致，省得读的人去想"为什么这一条不一样"
    return {
        "language": counts(sa.cast(BenchmarkTask.issue_language, sa.Text)),
        "difficulty": counts(sa.cast(BenchmarkTask.difficulty, sa.Text)),
        "repository": counts(
            sa.cast(Repository.full_name, sa.Text),
            (Repository, Repository.id == BenchmarkTask.repository_id),
        ),
    }


__all__ = ["BenchmarkSetDetail", "BenchmarkSetSummary", "CompositionCell", "router"]
