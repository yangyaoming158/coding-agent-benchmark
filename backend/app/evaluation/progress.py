"""实验进度聚合（E5-T2）。

把 `evaluation_task_runs` 里那一堆行，算成 `evaluation_runs` 上的十几个数：
解决率、平台故障率、成本、token、重试次数、makespan。

## 三条口径，混了就全错（协议 C-56）

| 算什么 | 用哪些 attempt |
|:---|:---|
| 解决率（严格 / 有效） | **只看 canonical attempt** |
| 成本、Token、总耗时 | **累计全部 attempt** |
| 平台故障率 | 只看 canonical attempt |

`retry_count` 是 attempt 总数减题数，`recovered_infra_failure_count` 是
"重试之后恢复正常的平台故障次数"。这两个数要在报告里单独展示：解决率同样是 40%，
零重试的和重试 30 次才凑齐的，可信度完全不同。

## 为什么是重算，不是累加

每道题跑完都会调一次 `refresh()`，8 条作业同时收尾就是 8 个事务同时更新同一行。
累加（`completed_tasks += 1`）在并发下会丢更新，而且一旦漏掉或者重复执行一次，
误差永远留在那里，没有任何办法事后发现。

重算是**幂等**的：输入是那张表的全部行，输出只由输入决定。多算几次、少算一次、
并发算，结果都一样。代价是每次要扫这次实验的几百行——库里一共也就几万行，
毫秒级，不值得为它冒丢数的风险。

## `TEST_TIMEOUT` 现在按平台故障算

协议 C-20 规定测试超时的责任归属要跑一次"不打补丁的对照组"才能定，
而对照组执行还没实现（E4-T4 的交付说明里写明留给后续任务）。

在此之前这里保守算作平台故障，同时用 `pending_control_run` 单独报数。
保守的方向是让实验更容易被判成 `PARTIAL`（进不了排行榜）——
反过来"当成 AI 的锅"会让平台故障率虚低，那是往有利于自己的方向猜。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.domain.enums import (
    AgentOutcome,
    EvaluationRunStatus,
    InfraOutcome,
    JobState,
    JobType,
)
from app.domain.protocol import (
    INFRA_TO_AGENT_MAPPING,
    InfraFailureCounting,
    max_allowed_infra_failures,
)
from app.infrastructure.logging import get_logger
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun
from app.infrastructure.models.job import JobQueue

logger = get_logger(__name__)

#: 有效解决率的分母（协议 C-21）：我们确实拿到了一个可以归因于被测 AI 的结果。
#: `NOT_ATTEMPTED` 和 NULL 不在里面 —— 那两种是"没给 AI 机会"或"没拿到结论"。
ATTRIBUTABLE_OUTCOMES = frozenset(
    {
        AgentOutcome.RESOLVED,
        AgentOutcome.UNRESOLVED,
        AgentOutcome.EMPTY_PATCH,
        AgentOutcome.INVALID_PATCH,
    }
)

#: 解决率列是 `Numeric(6, 4)`，多出来的位数写不进去。
_RATE_PLACES = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class AttemptRow:
    """聚合要用到的字段。刻意不用 ORM 对象：这样纯函数可以单测，不用起数据库。"""

    benchmark_task_id: int
    attempt_no: int
    is_canonical: bool
    infra_outcome: InfraOutcome | None
    agent_outcome: AgentOutcome | None
    cost_usd: Decimal | None
    tokens_total: int | None
    prepare_started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class RunProgress:
    """一次实验此刻的全部统计。字段和 `evaluation_runs` 的列一一对应。"""

    total_tasks: int
    #: 已经有认定结果（canonical attempt）的题数。没有结论的题不算完成。
    completed_tasks: int
    resolved_count: int
    infra_failure_count: int
    #: 其中要等 C-20 对照组才能定责的题数。它已经被算进 `infra_failure_count` 了，
    #: 这里再报一次是为了让人知道那个数字里有多少是"暂定"的。
    pending_control_run: int
    strict_resolve_rate: Decimal | None
    effective_resolve_rate: Decimal | None
    total_cost_usd: Decimal
    total_tokens: int
    retry_count: int
    recovered_infra_failure_count: int
    makespan_ms: int | None
    status: EvaluationRunStatus

    @property
    def leaderboard_eligible(self) -> bool:
        """能不能进排行榜（协议 C-26）。`PARTIAL` 一律不能。"""
        return self.status is EvaluationRunStatus.COMPLETED


def counts_as_infra_failure(outcome: InfraOutcome) -> bool:
    """这个故障算不算平台故障（协议 C-18 的"计入平台故障率"列）。

    `BY_CONTROL_RUN`（只有 `TEST_TIMEOUT`）在对照组执行做出来之前保守算作**是**，
    理由见模块开头。
    """
    counting = INFRA_TO_AGENT_MAPPING[outcome].counts_as_infra_failure
    return counting in (InfraFailureCounting.YES, InfraFailureCounting.BY_CONTROL_RUN)


def needs_control_run(outcome: InfraOutcome) -> bool:
    """这个结论要跑完 C-20 的对照组才能定责。"""
    return (
        INFRA_TO_AGENT_MAPPING[outcome].counts_as_infra_failure
        is InfraFailureCounting.BY_CONTROL_RUN
    )


def summarize(
    rows: Sequence[AttemptRow],
    *,
    total_tasks: int,
    all_jobs_done: bool,
    current_status: EvaluationRunStatus,
) -> RunProgress:
    """把 attempt 明细算成一次实验的统计。**纯函数**，同样的输入永远同样的输出。

    `all_jobs_done` 表示这次实验在 `job_queue` 里已经没有活作业了 ——
    它决定要不要定案（`COMPLETED` / `PARTIAL`），由调用方查库得出。
    """
    canonical = [r for r in rows if r.is_canonical]
    resolved = sum(1 for r in canonical if r.agent_outcome is AgentOutcome.RESOLVED)
    infra_failed = sum(
        1
        for r in canonical
        if r.infra_outcome is not None and counts_as_infra_failure(r.infra_outcome)
    )
    pending_control = sum(
        1 for r in canonical if r.infra_outcome is not None and needs_control_run(r.infra_outcome)
    )
    attributable = sum(1 for r in canonical if r.agent_outcome in ATTRIBUTABLE_OUTCOMES)

    # 成本和 token 累计**全部** attempt（C-56）：重试也是真金白银花掉的
    cost = sum((r.cost_usd for r in rows if r.cost_usd is not None), Decimal(0))
    tokens = sum(r.tokens_total or 0 for r in rows)

    tasks_seen = {r.benchmark_task_id for r in rows}
    retry_count = len(rows) - len(tasks_seen)

    status = _next_status(
        current_status,
        all_jobs_done=all_jobs_done,
        total_tasks=total_tasks,
        completed_tasks=len(canonical),
        infra_failed=infra_failed,
    )
    return RunProgress(
        total_tasks=total_tasks,
        completed_tasks=len(canonical),
        resolved_count=resolved,
        infra_failure_count=infra_failed,
        pending_control_run=pending_control,
        strict_resolve_rate=_rate(resolved, total_tasks),
        effective_resolve_rate=_rate(resolved, attributable),
        total_cost_usd=cost,
        total_tokens=tokens,
        retry_count=retry_count,
        recovered_infra_failure_count=_recovered(rows),
        makespan_ms=_makespan_ms(rows),
        status=status,
    )


def _rate(numerator: int, denominator: int) -> Decimal | None:
    """解决率。分母为 0 时是 None，不是 0 —— "一道题都没有"和"一道都没解决"不是一回事。"""
    if denominator <= 0:
        return None
    return (Decimal(numerator) / Decimal(denominator)).quantize(_RATE_PLACES)


def _recovered(rows: Sequence[AttemptRow]) -> int:
    """重试之后恢复正常的平台故障次数（协议 C-56）。

    按题看：这道题最后认定的结果不是平台故障，但前面出现过平台故障 ——
    那些故障就是"被重试救回来的"。数的是**次数**不是题数，
    一道题重试两次才成功就记 2。
    """
    by_task: dict[int, list[AttemptRow]] = {}
    for row in rows:
        by_task.setdefault(row.benchmark_task_id, []).append(row)

    recovered = 0
    for attempts in by_task.values():
        final = next((a for a in attempts if a.is_canonical), None)
        if final is None or final.infra_outcome is None:
            continue
        if counts_as_infra_failure(final.infra_outcome):
            continue  # 最后还是挂了，不算恢复
        recovered += sum(
            1
            for a in attempts
            if a is not final
            and a.infra_outcome is not None
            and counts_as_infra_failure(a.infra_outcome)
        )
    return recovered


def _makespan_ms(rows: Sequence[AttemptRow]) -> int | None:
    """第一道题开始到最后一道题结束的墙钟时间，**不是**各题耗时之和。

    并发跑的时候两者差得很远：8 道题各花 10 分钟，串行是 80 分钟，
    并发可能只有 12 分钟。报告里要的是后者。
    """
    starts = [r.prepare_started_at for r in rows if r.prepare_started_at is not None]
    ends = [r.completed_at for r in rows if r.completed_at is not None]
    if not starts or not ends:
        return None
    return max(int((max(ends) - min(starts)).total_seconds() * 1000), 0)


def _next_status(
    current: EvaluationRunStatus,
    *,
    all_jobs_done: bool,
    total_tasks: int,
    completed_tasks: int,
    infra_failed: int,
) -> EvaluationRunStatus:
    """算实验现在该是什么状态。

    两个终态不再变：`COMPLETED` 是已经定案的结论，`CANCELLED` 是人做的决定。
    `PARTIAL` 可以再变 —— 补跑（`retry-failed`）把缺的题填上之后，
    它应该能升回 `COMPLETED`。
    """
    if current in (EvaluationRunStatus.COMPLETED, EvaluationRunStatus.CANCELLED):
        return current
    if not all_jobs_done:
        return EvaluationRunStatus.RUNNING
    if completed_tasks < total_tasks:
        # 有题一个结论都没有（作业被判 DEAD、或者取消之后没补跑）。
        # 这种实验不完整，不能算 COMPLETED，更不能进排行榜
        return EvaluationRunStatus.PARTIAL
    if infra_failed > max_allowed_infra_failures(total_tasks):
        return EvaluationRunStatus.PARTIAL  # C-26：平台故障超标，降级
    return EvaluationRunStatus.COMPLETED


# ── 查库那一半 ──────────────────────────────────────────────


def load_attempts(session: Session, evaluation_run_id: int) -> list[AttemptRow]:
    """把一次实验的全部 attempt 读成纯数据。"""
    rows = session.execute(
        sa.select(
            EvaluationTaskRun.benchmark_task_id,
            EvaluationTaskRun.attempt_no,
            EvaluationTaskRun.is_canonical,
            EvaluationTaskRun.infra_outcome,
            EvaluationTaskRun.agent_outcome,
            EvaluationTaskRun.cost_usd,
            EvaluationTaskRun.tokens_total,
            EvaluationTaskRun.prepare_started_at,
            EvaluationTaskRun.completed_at,
        )
        .where(EvaluationTaskRun.evaluation_run_id == evaluation_run_id)
        .order_by(EvaluationTaskRun.benchmark_task_id, EvaluationTaskRun.attempt_no)
    ).all()
    return [AttemptRow(*row) for row in rows]


def live_job_count(session: Session, evaluation_run_id: int, *, exclude_job_id: int | None) -> int:
    """这次实验还有几条作业没走完（PENDING 或 LEASED）。

    `exclude_job_id` 用来排掉**调用方自己那条**：进度聚合跑在作业收尾的同一个事务里，
    那时这条作业还挂在 LEASED 上（`ctx.complete()` 是先写业务、后标 DONE）。
    不排掉的话，最后一道题永远发现"还有一条在跑"，实验就定不了案。
    """
    stmt = sa.select(sa.func.count()).where(
        JobQueue.job_type == JobType.EVAL_TASK,
        JobQueue.state.in_([JobState.PENDING, JobState.LEASED]),
        JobQueue.payload["evaluation_run_id"].astext == str(evaluation_run_id),
    )
    if exclude_job_id is not None:
        stmt = stmt.where(JobQueue.id != exclude_job_id)
    return int(session.execute(stmt).scalar_one())


def lock_run(session: Session, evaluation_run_id: int) -> EvaluationRun | None:
    """锁住实验那一行（`SELECT ... FOR UPDATE`）。实验不存在返回 None。

    ⚠️ **要写这次实验的任何子表之前，先调它。** 顺序反了会死锁，
    而且是在并发跑起来之后才会撞上的那种死锁 —— 2026-09-06 的 8 槽位实测里
    真的撞了一次（作业 #133，`DeadlockDetected`，白等了 60 秒的退避才重试成功）。

    为什么会死锁：往 `evaluation_task_runs` 插一行时，Postgres 会顺手在它的父行
    （`evaluation_runs`）上加一把 `FOR KEY SHARE` 锁，防止父行在事务中途被删。
    这把锁**互相兼容**，所以两条作业可以同时持有。等它们各自再去要 `FOR UPDATE`
    时，就成了两边都在等对方放开 KEY SHARE —— 教科书式的锁升级死锁。

    先拿 `FOR UPDATE` 就没有升级这一步：谁先拿到谁做完，另一个在门口等，
    毫秒级的事。
    """
    return session.execute(
        sa.select(EvaluationRun).where(EvaluationRun.id == evaluation_run_id).with_for_update()
    ).scalar_one_or_none()


def refresh(
    session: Session, evaluation_run_id: int, *, exclude_job_id: int | None = None
) -> RunProgress | None:
    """重算一次实验的统计并写回 `evaluation_runs`。**不 commit**。

    实验不存在返回 None（题被删了、库被清了，不该让作业跟着失败）。

    锁的粒度是一行、持有时间是毫秒级。不锁的话，8 条作业同时收尾时
    状态可能被算得早的那次覆盖回去。
    """
    run = lock_run(session, evaluation_run_id)
    if run is None:
        return None

    progress = summarize(
        load_attempts(session, evaluation_run_id),
        total_tasks=run.total_tasks,
        all_jobs_done=live_job_count(session, evaluation_run_id, exclude_job_id=exclude_job_id)
        == 0,
        current_status=run.status,
    )
    apply_to(run, progress)
    return progress


def apply_to(run: EvaluationRun, progress: RunProgress) -> None:
    """把算出来的统计写进 ORM 对象。

    `finished_at` 只在第一次进终态时写。反复写的话，"这次实验什么时候结束的"
    会变成"最后一次有人碰它是什么时候"。
    """
    was_terminal = run.status in _TERMINAL_RUN_STATUSES
    run.completed_tasks = progress.completed_tasks
    run.resolved_count = progress.resolved_count
    run.infra_failure_count = progress.infra_failure_count
    run.strict_resolve_rate = progress.strict_resolve_rate
    run.effective_resolve_rate = progress.effective_resolve_rate
    run.total_cost_usd = progress.total_cost_usd
    run.total_tokens = progress.total_tokens
    run.retry_count = progress.retry_count
    run.recovered_infra_failure_count = progress.recovered_infra_failure_count
    run.makespan_ms = progress.makespan_ms
    run.status = progress.status
    if progress.status in _TERMINAL_RUN_STATUSES and not was_terminal:
        run.finished_at = datetime.now(tz=UTC)
        logger.info(
            "evaluation_run_finalized",
            evaluation_run_id=run.id,
            status=progress.status.value,
            resolved=f"{progress.resolved_count}/{progress.total_tasks}",
            infra_failures=progress.infra_failure_count,
            pending_control_run=progress.pending_control_run,
            retry_count=progress.retry_count,
        )


#: 实验的终态。到了这里就不再自己往下走了（`PARTIAL` 例外，补跑能把它升回来）。
_TERMINAL_RUN_STATUSES = frozenset(
    {
        EvaluationRunStatus.COMPLETED,
        EvaluationRunStatus.PARTIAL,
        EvaluationRunStatus.CANCELLED,
        EvaluationRunStatus.FAILED,
    }
)


__all__ = [
    "ATTRIBUTABLE_OUTCOMES",
    "AttemptRow",
    "RunProgress",
    "apply_to",
    "counts_as_infra_failure",
    "live_job_count",
    "load_attempts",
    "lock_run",
    "needs_control_run",
    "refresh",
    "summarize",
]
