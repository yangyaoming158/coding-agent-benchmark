"""`report_records` 的分组口径（E7-T9）：三种格式合并成一批，不能重复列。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.enums import ReportFormat, ReportScope
from app.report.listing import ReportRecordRow, group_report_records


def _row(
    record_id: int,
    *,
    run_id: int,
    generated_at: datetime,
    report_format: ReportFormat,
    run_ids: tuple[int, ...] | None = None,
    scope: ReportScope = ReportScope.SINGLE_RUN,
) -> ReportRecordRow:
    return ReportRecordRow(
        id=record_id,
        evaluation_run_id=run_id,
        scope=scope,
        run_ids=run_ids or (run_id,),
        format=report_format,
        generated_at=generated_at,
    )


def test_three_formats_from_the_same_generation_become_one_batch() -> None:
    stamp = datetime(2026, 9, 20, tzinfo=UTC)
    rows = [
        _row(10, run_id=1, generated_at=stamp, report_format=ReportFormat.HTML),
        _row(11, run_id=1, generated_at=stamp, report_format=ReportFormat.MARKDOWN),
        _row(12, run_id=1, generated_at=stamp, report_format=ReportFormat.JSON),
    ]

    batches = group_report_records(rows)

    assert len(batches) == 1
    batch = batches[0]
    assert batch.id == 10  # 组内最小的 id
    assert batch.run_ids == (1,)
    assert dict(batch.formats) == {
        ReportFormat.HTML: 10,
        ReportFormat.JSON: 12,
        ReportFormat.MARKDOWN: 11,
    }


def test_different_generation_times_are_different_batches_even_for_the_same_run() -> None:
    """同一个实验被生成过两次报告（比如改了 top_n 重跑）不能被误合并成一批。"""
    older = datetime(2026, 9, 19, tzinfo=UTC)
    newer = datetime(2026, 9, 20, tzinfo=UTC)
    rows = [
        _row(1, run_id=5, generated_at=older, report_format=ReportFormat.HTML),
        _row(2, run_id=5, generated_at=newer, report_format=ReportFormat.HTML),
    ]

    batches = group_report_records(rows)

    assert len(batches) == 2


def test_batches_sort_newest_generated_first() -> None:
    older = datetime(2026, 9, 19, tzinfo=UTC)
    newer = datetime(2026, 9, 21, tzinfo=UTC)
    rows = [
        _row(1, run_id=1, generated_at=older, report_format=ReportFormat.HTML),
        _row(2, run_id=2, generated_at=newer, report_format=ReportFormat.HTML),
    ]

    batches = group_report_records(rows)

    assert [batch.run_ids for batch in batches] == [(2,), (1,)]


def test_comparison_scope_keeps_all_run_ids_together() -> None:
    stamp = datetime(2026, 9, 20, tzinfo=UTC)
    rows = [
        _row(
            1,
            run_id=1,
            generated_at=stamp,
            report_format=ReportFormat.HTML,
            run_ids=(1, 2, 3),
            scope=ReportScope.COMPARISON,
        ),
        _row(
            2,
            run_id=1,
            generated_at=stamp,
            report_format=ReportFormat.JSON,
            run_ids=(1, 2, 3),
            scope=ReportScope.COMPARISON,
        ),
    ]

    (batch,) = group_report_records(rows)

    assert batch.scope is ReportScope.COMPARISON
    assert batch.run_ids == (1, 2, 3)


def test_no_records_means_no_batches() -> None:
    assert group_report_records([]) == []
