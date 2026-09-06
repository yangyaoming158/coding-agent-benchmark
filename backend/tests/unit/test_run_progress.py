"""实验进度聚合的算术（E5-T2）。

`summarize()` 是纯函数，所以协议 C-21 和 C-56 的每一条口径都能写成断言，
不用起数据库、不用跑评测。

盯住的是三件最容易算错的事：

1. **解决率只数 canonical attempt，成本却要累计全部 attempt**（C-56）。
   混起来算的话，重试过的题会被重复计入解决率的分母。
2. **有效解决率的分母不是"跑成功的题"**（C-21 的 v1.0 修正记录）：
   `AGENT_TIMEOUT` 和 `INVALID_PATCH` 都是被测 AI 自己的失败，要留在分母里。
3. **平台故障超标就是 PARTIAL**（C-26/C-26a），不能进排行榜。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.enums import AgentOutcome, EvaluationRunStatus, InfraOutcome
from app.evaluation.progress import AttemptRow, summarize

START = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)


def row(
    task: int,
    *,
    attempt: int = 1,
    canonical: bool = True,
    infra: InfraOutcome = InfraOutcome.SUCCESS,
    agent: AgentOutcome | None = AgentOutcome.RESOLVED,
    cost: str | None = "0.01",
    tokens: int | None = 1000,
    start_min: int = 0,
    end_min: int = 1,
) -> AttemptRow:
    return AttemptRow(
        benchmark_task_id=task,
        attempt_no=attempt,
        is_canonical=canonical,
        infra_outcome=infra,
        agent_outcome=agent,
        cost_usd=None if cost is None else Decimal(cost),
        tokens_total=tokens,
        prepare_started_at=START + timedelta(minutes=start_min),
        completed_at=START + timedelta(minutes=end_min),
    )


def summarize_done(rows: list[AttemptRow], *, total: int) -> object:
    return summarize(
        rows,
        total_tasks=total,
        all_jobs_done=True,
        current_status=EvaluationRunStatus.RUNNING,
    )


# ── C-21：两个解决率 ────────────────────────────────────────


def test_strict_rate_divides_by_every_task_in_the_set() -> None:
    """严格解决率的分母是题库全部题数，哪怕有的题根本没跑出结论。

    分母改成"跑出结论的题数"会让解决率虚高：平台故障越多，分母越小，
    数字反而越好看。"""
    rows = [row(1), row(2, agent=AgentOutcome.UNRESOLVED)]
    result = summarize_done(rows, total=4)
    assert result.strict_resolve_rate == Decimal("0.2500")


def test_effective_rate_keeps_the_agents_own_failures_in_the_denominator() -> None:
    """`AGENT_TIMEOUT` 是 AI 自己超时，不是平台故障，必须留在有效解决率的分母里。

    这是协议 v1.0 修正过的一处：原来用 `infra_outcome = SUCCESS` 当分母，
    会把 AI 自己的失败排除掉，解决率虚高。
    """
    rows = [
        row(1),
        row(2, infra=InfraOutcome.AGENT_TIMEOUT, agent=AgentOutcome.UNRESOLVED),
        # 平台故障：AI 压根没启动，不该进有效解决率的分母
        row(3, infra=InfraOutcome.WORKSPACE_ERROR, agent=AgentOutcome.NOT_ATTEMPTED),
    ]
    result = summarize_done(rows, total=3)
    assert result.effective_resolve_rate == Decimal("0.5000")  # 1 / 2
    assert result.strict_resolve_rate == Decimal("0.3333")  # 1 / 3


def test_rates_are_none_when_there_is_nothing_to_divide_by() -> None:
    """一道题都没有时是 None，不是 0 —— "没有题"和"一道都没解决"不是一回事。"""
    result = summarize_done([], total=0)
    assert result.strict_resolve_rate is None
    assert result.effective_resolve_rate is None


# ── C-56：三条口径分开 ──────────────────────────────────────


def test_cost_and_tokens_accumulate_every_attempt() -> None:
    """成本累计**全部** attempt，解决率只看 canonical。

    重试也是真金白银花掉的。只算 canonical 的话，一道重试三次才成功的题，
    报出来的成本只有实际的三分之一。
    """
    rows = [
        row(1, attempt=1, canonical=False, infra=InfraOutcome.ENV_BUILD_FAILED, agent=None),
        row(1, attempt=2, canonical=True),
    ]
    result = summarize_done(rows, total=1)
    assert result.total_cost_usd == Decimal("0.02"), "两次 attempt 的钱都要算"
    assert result.total_tokens == 2000
    assert result.resolved_count == 1, "解决率只认那一条 canonical"
    assert result.completed_tasks == 1


def test_retry_count_is_attempts_minus_tasks() -> None:
    rows = [
        row(1, attempt=1, canonical=False, infra=InfraOutcome.SANDBOX_ERROR, agent=None),
        row(1, attempt=2, canonical=False, infra=InfraOutcome.SANDBOX_ERROR, agent=None),
        row(1, attempt=3, canonical=True),
        row(2, attempt=1, canonical=True),
    ]
    result = summarize_done(rows, total=2)
    assert result.retry_count == 2


def test_recovered_counts_platform_failures_that_retrying_saved() -> None:
    """重试救回来的平台故障要单独报数（C-56）。

    解决率同样是 50%，零重试的和"重试了一堆才凑齐"的，可信度完全不同。
    """
    rows = [
        row(1, attempt=1, canonical=False, infra=InfraOutcome.SANDBOX_ERROR, agent=None),
        row(1, attempt=2, canonical=True),  # 救回来了
        row(2, attempt=1, canonical=False, infra=InfraOutcome.SANDBOX_ERROR, agent=None),
        row(2, attempt=2, canonical=True, infra=InfraOutcome.SANDBOX_ERROR, agent=None),  # 没救回来
    ]
    result = summarize_done(rows, total=2)
    assert result.recovered_infra_failure_count == 1
    assert result.infra_failure_count == 1, "没救回来的那题才算平台故障"


def test_makespan_is_wall_clock_not_the_sum_of_task_durations() -> None:
    """makespan 是"第一道题开始到最后一道题结束"，不是各题耗时相加。

    并发跑的时候两者差得很远，报出总和等于在说"我们其实是串行的"。
    """
    rows = [row(1, start_min=0, end_min=10), row(2, start_min=1, end_min=12)]
    result = summarize_done(rows, total=2)
    assert result.makespan_ms == 12 * 60 * 1000


# ── C-26：排行榜准入 ────────────────────────────────────────


def test_a_clean_run_completes() -> None:
    result = summarize_done([row(i) for i in range(1, 21)], total=20)
    assert result.status is EvaluationRunStatus.COMPLETED
    assert result.leaderboard_eligible is True


def test_too_many_platform_failures_downgrade_the_run_to_partial() -> None:
    """20 题最多允许 1 题平台故障（floor(20 × 5%)），2 题就要降级。"""
    rows = [row(i) for i in range(1, 19)]
    rows += [
        row(19, infra=InfraOutcome.WORKSPACE_ERROR, agent=AgentOutcome.NOT_ATTEMPTED),
        row(20, infra=InfraOutcome.WORKSPACE_ERROR, agent=AgentOutcome.NOT_ATTEMPTED),
    ]
    result = summarize_done(rows, total=20)
    assert result.infra_failure_count == 2
    assert result.status is EvaluationRunStatus.PARTIAL
    assert result.leaderboard_eligible is False


def test_exactly_at_the_threshold_still_completes() -> None:
    """协议 C-26 写得很死：**大于** 5% 才不准入，正好等于可以进。"""
    rows = [row(i) for i in range(1, 20)]
    rows.append(row(20, infra=InfraOutcome.WORKSPACE_ERROR, agent=AgentOutcome.NOT_ATTEMPTED))
    assert summarize_done(rows, total=20).status is EvaluationRunStatus.COMPLETED


def test_a_task_without_any_verdict_keeps_the_run_out_of_completed() -> None:
    """作业死光了、某道题一条结论都没有 → 这次实验不完整，只能是 PARTIAL。

    算成 COMPLETED 的话，那道题会被当成"跑了但没解决"，
    而实际上它根本没跑过 —— 解决率的分母还在，分子却永远少一块。
    """
    result = summarize_done([row(1), row(2)], total=3)
    assert result.completed_tasks == 2
    assert result.status is EvaluationRunStatus.PARTIAL


def test_a_run_with_live_jobs_stays_running() -> None:
    result = summarize(
        [row(1)],
        total_tasks=3,
        all_jobs_done=False,
        current_status=EvaluationRunStatus.RUNNING,
    )
    assert result.status is EvaluationRunStatus.RUNNING


def test_cancelled_and_completed_are_sticky() -> None:
    """已经定案的实验不会被重算改掉。

    `CANCELLED` 是人做的决定，`COMPLETED` 是已经发布的结论 ——
    协议 C-55 禁止修改已发布实验的结果。
    """
    for status in (EvaluationRunStatus.CANCELLED, EvaluationRunStatus.COMPLETED):
        result = summarize([row(1)], total_tasks=9, all_jobs_done=True, current_status=status)
        assert result.status is status


# ── C-20 还没做，测试超时先保守算 ───────────────────────────


def test_test_timeout_is_counted_as_a_platform_failure_and_flagged() -> None:
    """`TEST_TIMEOUT` 的责任要跑 C-20 的对照组才能定，对照组还没实现。

    在此之前保守算作平台故障（实验更容易被判 PARTIAL、进不了排行榜），
    同时用 `pending_control_run` 单独报数，让人知道这个数字里有多少是暂定的。
    反过来算成"AI 的锅"会让平台故障率虚低 —— 那是往有利于自己的方向猜。
    """
    rows = [row(1), row(2, infra=InfraOutcome.TEST_TIMEOUT, agent=None)]
    result = summarize_done(rows, total=2)
    assert result.infra_failure_count == 1
    assert result.pending_control_run == 1
