"""重灌规程的最后一步：把没人审过的题退回 REVIEW_REQUIRED（E9-T2）。

为什么会需要这一步：`promote assemble` 会把**全部**探测通过的候选组装成题，
八步验证又会把跑得通的一律置 VALID。而 E8-T2 的人工终审只覆盖了其中一部分 ——
2026-09-12 重灌时组装出 51 道，终审名单只有 31 道，多出来的 20 道**没人看过**
却也是 VALID，`dataset stage` 会把它们一起冻进快照，快照摘要和已发布的
`benchmark-dev@v1` 对不上，而且不报错。

这一组只测"名单是怎么算出来的"（纯函数）。改库那半要数据库，在集成测试里。
"""

from __future__ import annotations

import csv
from pathlib import Path

from cli.promote import _is_mined, reviewed_task_ids

COLUMNS = ["task_id", "verdict", "reason"]


def _csv(path: Path, rows: list[tuple[str, str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        writer.writerows(rows)
    return path


def test_only_rows_with_a_verdict_count_as_reviewed(tmp_path: Path) -> None:
    """导出的表里没填 verdict 的那些**不算审过**。

    算进去的话，"导出过对照表"就等于"审过了"，而这两件事差着一个人的工作。
    """
    path = _csv(
        tmp_path / "review.csv",
        [("a-1", "ACCEPT", "好题"), ("a-2", "", ""), ("a-3", " REJECT ", "题面带答案")],
    )
    assert reviewed_task_ids([path]) == {"a-1", "a-3"}


def test_several_sheets_merge(tmp_path: Path) -> None:
    """终审分几次做就有几份表，名单是并集（benchmark-dev 就是两份）。"""
    first = _csv(tmp_path / "one.csv", [("a-1", "ACCEPT", "")])
    second = _csv(tmp_path / "two.csv", [("b-9", "ACCEPT", "")])
    assert reviewed_task_ids([first, second]) == {"a-1", "b-9"}


def test_golden_tasks_are_never_parked() -> None:
    """Golden 题不走人工终审，不能被这一步退回去 —— 退了哨兵测试就没题可跑。"""
    assert not _is_mined("bench-golden__auth-2")
    assert _is_mined("pallets__click-2271")
