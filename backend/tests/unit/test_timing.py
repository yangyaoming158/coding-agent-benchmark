"""阶段耗时统计（E9-T1）。

纯算术和纯取数，所以放单元测试里。`load_attempts()` 那条查询由集成测试覆盖
（`tests/integration/test_timing_persistence.py`）。

有一条是**回归测试**：`test_zero_agent_duration_is_not_treated_as_missing`。
写这个模块时踩过 —— `_ms_to_s(0) or _gap(...)` 里 `0.0` 是 falsy，于是 Oracle
真实的"Agent 阶段 0 毫秒"被当成"没测到"，悄悄换成两个时刻之差。
不会报错，只会让 Oracle 的 `A` 从 0 变成几毫秒，再被"反正很小"掩盖过去。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.enums import InfraOutcome
from app.evaluation.timing import (
    STAGES,
    Attempt,
    actual_makespan_minutes,
    percentile,
    summarize,
    summarize_stage,
    to_csv,
)
from app.evaluation.timing import _stages_of as stages_of
from app.infrastructure.models.evaluation import EvaluationTaskRun

BASE = datetime(2026, 9, 12, 10, 0, 0, tzinfo=UTC)


def _attempt(
    *,
    agent_name: str = "aider",
    agent_s: float | None = 300.0,
    total_s: float | None = 310.0,
    infra_outcome: InfraOutcome | None = InfraOutcome.SUCCESS,
    task_id: str = "pallets__click-3642",
    task_run_id: int = 1,
    start_offset_s: float = 0.0,
) -> Attempt:
    """造一条执行记录。`other` 按模块的口径算（total − agent）。"""
    other_s = None if total_s is None else total_s - (agent_s or 0.0)
    started = BASE + timedelta(seconds=start_offset_s)
    return Attempt(
        task_run_id=task_run_id,
        evaluation_run_id=200,
        agent_name=agent_name,
        task_id=task_id,
        attempt_no=1,
        infra_outcome=infra_outcome,
        started_at=started,
        finished_at=None if total_s is None else started + timedelta(seconds=total_s),
        stages={
            "queue": 1.0,
            "prepare": 5.0,
            "agent": agent_s,
            "handoff": 1.0,
            "test": 4.0,
            "judge": 0.5,
            "total": total_s,
            "other": other_s,
        },
    )


# ── 从时刻列取阶段 ──────────────────────────────────────────


def test_stages_come_from_the_timestamp_columns() -> None:
    """六个时刻列 → 八段耗时。"""
    row = EvaluationTaskRun(
        queued_at=BASE,
        prepare_started_at=BASE + timedelta(seconds=2),
        agent_started_at=BASE + timedelta(seconds=10),
        agent_finished_at=BASE + timedelta(seconds=310),
        test_started_at=BASE + timedelta(seconds=312),
        test_finished_at=BASE + timedelta(seconds=320),
        completed_at=BASE + timedelta(seconds=321),
    )
    stages = stages_of(row)

    assert stages["queue"] == pytest.approx(2.0)
    assert stages["prepare"] == pytest.approx(8.0)
    assert stages["agent"] == pytest.approx(300.0)
    assert stages["handoff"] == pytest.approx(2.0)
    assert stages["test"] == pytest.approx(8.0)
    assert stages["judge"] == pytest.approx(1.0)
    assert stages["total"] == pytest.approx(319.0)


def test_duration_columns_win_over_the_timestamps() -> None:
    """`*_duration_ms` 是适配器和沙箱层自己测的，比两个时刻相减更贴近容器真实耗时。"""
    row = EvaluationTaskRun(
        prepare_started_at=BASE,
        agent_started_at=BASE + timedelta(seconds=5),
        agent_finished_at=BASE + timedelta(seconds=400),
        completed_at=BASE + timedelta(seconds=420),
        agent_duration_ms=333_000,
        test_duration_ms=7_500,
        total_duration_ms=350_000,
    )
    stages = stages_of(row)

    assert stages["agent"] == pytest.approx(333.0)
    assert stages["test"] == pytest.approx(7.5)
    assert stages["total"] == pytest.approx(350.0)


def test_zero_agent_duration_is_not_treated_as_missing() -> None:
    """Oracle 的 Agent 阶段是 0 毫秒，这是真实取值，不是"没测到"。

    回归测试，见模块开头。`0.0 or fallback` 会取 fallback，把一个真实的 0
    换成两个时刻之差。
    """
    row = EvaluationTaskRun(
        prepare_started_at=BASE,
        agent_started_at=BASE + timedelta(seconds=2),
        agent_finished_at=BASE + timedelta(seconds=9),  # 起停容器的那几秒
        completed_at=BASE + timedelta(seconds=20),
        agent_duration_ms=0,
        total_duration_ms=8_400,
    )
    stages = stages_of(row)

    assert stages["agent"] == 0.0, "真实的 0 被兜底逻辑换掉了"
    # S = total − agent，Agent 是 0 的时候 S 就是整个 total
    assert stages["other"] == pytest.approx(8.4)


def test_missing_timestamps_give_none_not_zero() -> None:
    """没跑到那一步的阶段是 `None`。算成 0 会把均值拉低，而且不会报错。"""
    row = EvaluationTaskRun(prepare_started_at=BASE, agent_started_at=BASE + timedelta(seconds=3))
    stages = stages_of(row)

    assert stages["prepare"] == pytest.approx(3.0)
    assert stages["agent"] is None
    assert stages["test"] is None
    assert stages["total"] is None
    assert stages["other"] is None


def test_other_is_total_minus_agent_not_a_sum_of_stages() -> None:
    """`S` 用减法算，某一段的时刻没落库时那段时间也不会漏掉。

    这一行是"补丁打不上，测试一条都没跑"：`test_started_at` / `test_finished_at`
    是空的，于是 `test` 和 `judge` 两段算不出来。逐段相加只剩 prepare 的 2 秒，
    但那 20 秒墙钟是真的花掉了 —— 减法算的 `S` 包含它，逐段相加会漏掉。
    """
    row = EvaluationTaskRun(
        prepare_started_at=BASE,
        agent_started_at=BASE + timedelta(seconds=2),
        agent_finished_at=BASE + timedelta(seconds=10),
        completed_at=BASE + timedelta(seconds=30),
    )
    stages = stages_of(row)

    assert stages["total"] == pytest.approx(30.0)
    assert stages["agent"] == pytest.approx(8.0)
    assert stages["other"] == pytest.approx(22.0)
    assert stages["test"] is None
    assert stages["judge"] is None
    piecewise = sum(
        value
        for stage in ("prepare", "handoff", "test", "judge")
        if (value := stages[stage]) is not None
    )
    assert piecewise == pytest.approx(2.0)
    assert stages["other"] > piecewise, "减法算出来的 S 应该比逐段相加大"


# ── 百分位 ──────────────────────────────────────────────────


def test_percentile_is_nearest_rank() -> None:
    """§18.4 的原话是"排序后第 95% 那个值"，就是最近秩，不插值。"""
    values = [float(n) for n in range(1, 21)]  # 1..20

    assert percentile(values, 0.50) == 10.0  # ceil(0.5*20) = 10 → 第 10 个
    assert percentile(values, 0.95) == 19.0  # ceil(0.95*20) = 19 → 第 19 个
    assert percentile(values, 1.0) == 20.0


def test_percentile_of_a_single_sample_is_that_sample() -> None:
    assert percentile([7.5], 0.95) == 7.5


def test_percentile_of_nothing_is_zero_not_an_exception() -> None:
    """一个阶段没样本不该让整张报表打不出来。"""
    assert percentile([], 0.95) == 0.0


def test_summarize_stage_reports_all_four_numbers() -> None:
    stats = summarize_stage("agent", [10.0, 20.0, 30.0, 40.0])

    assert stats.samples == 4
    assert stats.mean_s == pytest.approx(25.0)
    assert stats.p50_s == 20.0
    assert stats.p95_s == 40.0
    assert stats.max_s == 40.0


# ── 按 Agent 聚合 ───────────────────────────────────────────


def test_summarize_groups_by_agent_and_sorts_by_name() -> None:
    attempts = [
        _attempt(agent_name="claude-code", task_run_id=1),
        _attempt(agent_name="aider", task_run_id=2),
        _attempt(agent_name="aider", task_run_id=3),
    ]
    result = summarize(attempts)

    assert [item.agent_name for item in result] == ["aider", "claude-code"]
    assert result[0].attempts == 2
    assert result[1].attempts == 1


def test_a_and_s_come_out_in_minutes() -> None:
    """`A` 和 `S` 的单位是分钟 —— §18.2 那张表用的就是分钟。"""
    attempts = [_attempt(agent_s=360.0, total_s=372.0)]
    (item,) = summarize(attempts)

    assert item.agent_minutes == pytest.approx(6.0)
    assert item.other_minutes == pytest.approx(0.2)


def test_timeout_rate_is_reported_separately() -> None:
    """超时率单列。一次超时往 `A` 里塞满一个 `agent_timeout_s`（click 是 720 秒）。"""
    attempts = [
        _attempt(agent_s=120.0, total_s=130.0, task_run_id=1),
        _attempt(agent_s=120.0, total_s=130.0, task_run_id=2),
        _attempt(
            agent_s=720.0,
            total_s=725.0,
            infra_outcome=InfraOutcome.AGENT_TIMEOUT,
            task_run_id=3,
        ),
    ]
    (item,) = summarize(attempts)

    assert item.timeouts == 1
    assert item.timeout_rate == pytest.approx(1 / 3)
    # 一次超时把 A 从 2 分钟拉到 5.3 分钟 —— 这就是为什么要单列
    assert item.agent_minutes == pytest.approx(320 / 60)


def test_retries_count_toward_the_mean() -> None:
    """全部 attempt 都算，不只 canonical。重试实打实占了机器。"""
    attempts = [
        _attempt(agent_s=60.0, total_s=70.0, task_run_id=1),
        _attempt(agent_s=60.0, total_s=70.0, task_run_id=2),  # 同一道题的第二次
    ]
    (item,) = summarize(attempts)
    assert item.attempts == 2
    assert item.stats["agent"].samples == 2


def test_stage_with_no_samples_is_absent_not_zero() -> None:
    """一个 Agent 完全没跑到测试阶段时，`test` 这一行不该出现在统计里。

    出现一个 `samples=0, mean=0` 的行，读起来就是"测试只花了 0 秒"。
    """
    attempt = _attempt()
    attempt.stages["test"] = None
    (item,) = summarize([attempt])

    assert "test" not in item.stats
    assert "agent" in item.stats


def test_summarize_of_nothing_is_empty() -> None:
    assert summarize([]) == []


# ── 实测 makespan ───────────────────────────────────────────


def test_actual_makespan_spans_all_runs() -> None:
    """跨多个实验算一个总数：最后一个结束减最早一个开始。"""
    attempts = [
        _attempt(task_run_id=1, start_offset_s=0.0, total_s=600.0),
        _attempt(task_run_id=2, start_offset_s=300.0, total_s=600.0),  # 结束在 900 秒
    ]
    assert actual_makespan_minutes(attempts) == pytest.approx(15.0)


def test_actual_makespan_needs_both_ends() -> None:
    """一条都没跑完的时候返回 `None`，不返回 0。"""
    assert actual_makespan_minutes([_attempt(total_s=None)]) is None
    assert actual_makespan_minutes([]) is None


# ── CSV ─────────────────────────────────────────────────────


def test_csv_has_one_row_per_attempt_and_a_column_per_stage() -> None:
    rows = to_csv([_attempt(task_run_id=7)]).strip().splitlines()

    header, body = rows[0].split(","), rows[1].split(",")
    assert header[:6] == [
        "task_run_id",
        "evaluation_run_id",
        "agent",
        "task_id",
        "attempt_no",
        "infra_outcome",
    ]
    assert header[6:] == list(STAGES)
    assert body[0] == "7"
    assert body[2] == "aider"
    assert body[5] == "SUCCESS"


def test_csv_leaves_missing_stages_empty() -> None:
    """取不到的阶段留空，不写 0 —— 读 CSV 的人分得清"没测到"和"0 秒"。"""
    attempt = _attempt(agent_s=None, total_s=None)
    body = to_csv([attempt]).strip().splitlines()[1].split(",")
    agent_column = 6 + STAGES.index("agent")

    assert body[agent_column] == ""
