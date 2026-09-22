"""`report_records` 的列表口径（E7-T9）。

一次 `generate_report` 产出三行 `report_records`（HTML / Markdown / JSON），
共享同一个 `evaluation_run_id`（锚点实验）和 `generated_at`（见
`app/report/service.py::persist_report`）。这里把三行合并成"一批报告"再列出去，
否则前端会看到同一次生成重复三条。

`report_records` 表现在只有几十行（一次生成三行，跑过的报告命令也就十来次），
所以分组和分页都在应用层做，不追求 SQL 层分页——量级变大之后再优化。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.domain.enums import ReportFormat, ReportScope
from app.infrastructure.models.agent import AgentConfig
from app.infrastructure.models.attribution import ReportRecord
from app.infrastructure.models.benchmark import BenchmarkSet
from app.infrastructure.models.evaluation import EvaluationRun


@dataclass(frozen=True, slots=True)
class ReportRecordRow:
    """`report_records` 一行的纯数据形状，脱离 ORM 方便单测分组逻辑。"""

    id: int
    evaluation_run_id: int | None
    scope: ReportScope
    run_ids: tuple[int, ...]
    format: ReportFormat
    generated_at: datetime


@dataclass(frozen=True, slots=True)
class ReportBatch:
    """同一次生成产出的一批报告（最多三种格式）。"""

    id: int
    """组内最小的 `report_records.id`；append-only 表，取最小值足够稳定。"""
    scope: ReportScope
    run_ids: tuple[int, ...]
    generated_at: datetime
    formats: tuple[tuple[ReportFormat, int], ...]
    """(格式, 对应的 report_record id) 按格式名排序，下载端点用这个 id 定位制品。"""


def group_report_records(rows: Sequence[ReportRecordRow]) -> list[ReportBatch]:
    """把三份格式的行合并成一批，按最新生成排在最前面。

    分组键是 `(evaluation_run_id, generated_at)`——两者由同一次
    `persist_report` 调用原样复制进三行，同一批次里完全相等；不用
    `run_ids` 做键是因为它是数组，某些数据库驱动比较数组相等没有整数稳。
    """
    groups: dict[tuple[int | None, datetime], list[ReportRecordRow]] = defaultdict(list)
    for row in rows:
        groups[(row.evaluation_run_id, row.generated_at)].append(row)

    batches = [
        ReportBatch(
            id=min(item.id for item in items),
            scope=items[0].scope,
            run_ids=items[0].run_ids,
            generated_at=items[0].generated_at,
            formats=tuple(sorted((item.format, item.id) for item in items)),
        )
        for items in groups.values()
    ]
    batches.sort(key=lambda batch: (batch.generated_at, batch.id), reverse=True)
    return batches


def dataset_labels_for_runs(session: Session, run_ids: Sequence[int]) -> dict[int, str]:
    """实验号 → "数据集@版本 · Agent 配置" 的展示标签，缺失的实验不报错、直接不出现在结果里。

    报告可能引用后来被删掉的实验（协议不禁止删除历史数据），列表页只是想展示
    "这批报告是谁跑的"，不是判定逻辑，容错比严格更重要。
    """
    if not run_ids:
        return {}
    rows = session.execute(
        sa.select(EvaluationRun.id, BenchmarkSet.slug, BenchmarkSet.version, AgentConfig.label)
        .join(AgentConfig, AgentConfig.id == EvaluationRun.agent_config_id)
        .join(BenchmarkSet, BenchmarkSet.id == EvaluationRun.benchmark_set_id)
        .where(EvaluationRun.id.in_(list(run_ids)))
    ).all()
    return {run_id: f"{slug}@{version} · {label}" for run_id, slug, version, label in rows}


def list_report_batches(
    session: Session, *, limit: int, offset: int
) -> tuple[list[ReportBatch], dict[int, str], int]:
    """查全部 `report_records`、分组、按最新排序后再切页。返回（本页批次，实验标签，总批次数）。"""
    rows = session.execute(
        sa.select(ReportRecord).order_by(ReportRecord.generated_at.desc(), ReportRecord.id.desc())
    ).scalars()
    records = [
        ReportRecordRow(
            id=row.id,
            evaluation_run_id=row.evaluation_run_id,
            scope=row.scope,
            run_ids=tuple(row.run_ids),
            format=row.format,
            generated_at=row.generated_at,
        )
        for row in rows
    ]
    batches = group_report_records(records)
    total = len(batches)
    page = batches[offset : offset + limit]
    all_run_ids = {run_id for batch in page for run_id in batch.run_ids}
    labels = dataset_labels_for_runs(session, sorted(all_run_ids))
    return page, labels, total


__all__ = [
    "ReportBatch",
    "ReportRecordRow",
    "dataset_labels_for_runs",
    "group_report_records",
    "list_report_batches",
]
