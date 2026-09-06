"""EvaluationRun 编排：建实验、取消、补跑、兜底定案（E5-T2）。

这一层只做四件事，都是**短事务里的纯数据库操作**，不碰 Docker、不起线程：

| 做什么 | 谁调 |
|:---|:---|
| `create_runs` 建实验并把题展开成作业 | `cli.experiment start`、以后的 `POST /api/runs` |
| `cancel_run` 取消 | `cli.experiment cancel`、`POST /api/runs/{id}/cancel` |
| `retry_failed` 把没结论的题补跑 | `cli.experiment retry-failed` |
| `finalize_stale_runs` 兜底定案 | Worker 主循环 |

真正跑题的是 Worker（`app.worker.handlers.eval_task`），这里只负责"排队"。

## 一次实验 = 一个 Agent 配置 × 一个数据集 × 一轮

多轮取样（同一批题跑 3 遍看波动）是**建 3 个实验**，不是在一个实验里跑 3 遍。
协议 C-55 要求人工重跑必须新建 `EvaluationRun`，而且 C-57 的部分唯一索引
限死了"每题至多一个 canonical attempt"——同一个实验里跑两遍同一道题，
第二遍的结论没有地方放。

## 补跑只补洞，不重跑已有结论的题

`retry_failed` 只处理"既没有认定结果、也没有在跑的作业"的题，也就是
作业被判 DEAD 或者取消留下的窟窿。已经有 canonical attempt 的题一律不碰：

- 协议 C-25 禁止取多次重试里"最好的一次"，重跑一道已经有结论的题，
  然后用新结果替换旧结果，正是这条禁止的做法；
- 协议 C-53 禁止人工判断触发自动重试（"这次不太对，再跑一遍"）；
- 协议 C-55 要求实验结束后的重跑必须新建实验。

所以 `COMPLETED` 的实验直接拒绝：它每道题都有结论，没有洞可补。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import CursorResult
from sqlalchemy.orm import Session

from app.domain.enums import EvaluationRunStatus, JobState, JobType, PatchKind
from app.domain.protocol import MAX_ATTEMPTS_PER_TASK
from app.evaluation import progress as progress_mod
from app.evaluation.jobs import EvalTaskPayload, enqueue_eval_task, normalized_patch_key
from app.infrastructure.logging import get_logger
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun, PatchArtifact
from app.infrastructure.models.job import JobQueue

logger = get_logger(__name__)

#: 还没走完的作业。取消、补跑、定案都要先问一句"这次实验还有没有活作业"。
LIVE_JOB_STATES = (JobState.PENDING, JobState.LEASED)


class OrchestrationError(RuntimeError):
    """编排层拒绝执行。消息是给人看的，直接打到终端上。"""


@dataclass(frozen=True, slots=True)
class CancelSummary:
    evaluation_run_id: int
    #: 还没被领走、直接掐掉的作业数。
    dropped_jobs: int
    #: 已经在跑的作业数。它们由各自的 Worker 中止，不在这里等。
    in_flight_jobs: int
    #: 这次实验之前就已经是 CANCELLED 了。
    already_cancelled: bool


@dataclass(frozen=True, slots=True)
class RetrySummary:
    evaluation_run_id: int
    #: 补投了作业的题（`benchmark_tasks.id`）。
    requeued: tuple[int, ...]
    #: attempt 数已经到 C-71 上限、不再补的题。
    at_attempt_cap: tuple[int, ...]
    #: 已经有认定结果、按 C-25 不碰的题数。
    already_decided: int
    #: 还有作业在排队或在跑的题数。
    still_running: int


# ── 建实验 ──────────────────────────────────────────────────


def create_runs(
    session: Session,
    *,
    name: str,
    benchmark_set_id: int,
    agent_config_id: int,
    task_ids: Sequence[int],
    agent_concurrency: int,
    sandbox_concurrency: int,
    rounds: int = 1,
    job_max_attempts: int = 3,
    created_by: str | None = None,
) -> list[EvaluationRun]:
    """建 `rounds` 个实验，每个都把 `task_ids` 展开成 EVAL_TASK 作业。**不 commit**。

    `total_tasks` 在这里就写死。它是严格解决率的分母（协议 C-21：
    "RESOLVED 的题数 / 题库里的全部题数"），跑的过程中不重算 ——
    重算的话，一道题因为作业死了没留下记录，分母会跟着缩水，解决率反而变好看。
    """
    if not task_ids:
        raise OrchestrationError("一道题都没选中，不建实验")
    if rounds < 1:
        raise OrchestrationError(f"轮数至少是 1，收到 {rounds}")

    runs: list[EvaluationRun] = []
    for index in range(1, rounds + 1):
        run = EvaluationRun(
            name=name if rounds == 1 else f"{name} · 第 {index} 轮",
            benchmark_set_id=benchmark_set_id,
            agent_config_id=agent_config_id,
            status=EvaluationRunStatus.QUEUED,
            agent_concurrency=agent_concurrency,
            sandbox_concurrency=sandbox_concurrency,
            total_tasks=len(task_ids),
            created_by=created_by,
        )
        session.add(run)
        session.flush()  # 要 id 去投作业
        for task_id in task_ids:
            enqueue_eval_task(
                session,
                evaluation_run_id=run.id,
                benchmark_task_id=task_id,
                max_attempts=job_max_attempts,
            )
        logger.info(
            "evaluation_run_created",
            evaluation_run_id=run.id,
            name=run.name,
            tasks=len(task_ids),
            agent_concurrency=agent_concurrency,
            sandbox_concurrency=sandbox_concurrency,
        )
        runs.append(run)
    return runs


def mark_running(session: Session, evaluation_run_id: int) -> EvaluationRunStatus | None:
    """第一道题开跑时把实验从 QUEUED 推到 RUNNING，返回它现在的状态。

    实验不存在返回 None。返回值里 `CANCELLED` 是要紧的那个：作业在队列里躺着的
    时候实验可能已经被取消了，处理函数拿到这个状态就直接收手，不用白跑十几分钟。

    `started_at` 只在第一次写。它要回答的是"这次实验什么时候开始的"，
    每道题都覆盖一次的话，最后留下的是最后一道题的开始时间。
    """
    run = session.execute(
        sa.select(EvaluationRun).where(EvaluationRun.id == evaluation_run_id).with_for_update()
    ).scalar_one_or_none()
    if run is None:
        return None
    if run.status in (EvaluationRunStatus.DRAFT, EvaluationRunStatus.QUEUED):
        run.status = EvaluationRunStatus.RUNNING
    if run.started_at is None:
        run.started_at = datetime.now(tz=UTC)
    return run.status


# ── 取消 ────────────────────────────────────────────────────


def cancel_run(session: Session, evaluation_run_id: int) -> CancelSummary:
    """取消一次实验。**不 commit**。

    这里只做两件立刻能做完的事：把实验标成 `CANCELLED`，把**还没被领走**的作业掐掉。

    已经在跑的那些不在这里等 —— 它们分散在各个 Worker 的线程里，这个函数
    可能是在另一台终端上跑的。Worker 自己的取消看门线程会在几秒内发现实验状态变了，
    杀掉容器、把那次执行记成 `CANCELLED`（见 `app.worker.cancel`）。

    没被领走的作业标成 `DEAD` 而不是删掉：留着才能事后回答"取消的时候还剩多少题没跑"。
    """
    run = session.execute(
        sa.select(EvaluationRun).where(EvaluationRun.id == evaluation_run_id).with_for_update()
    ).scalar_one_or_none()
    if run is None:
        raise OrchestrationError(f"找不到实验 #{evaluation_run_id}")
    if run.status is EvaluationRunStatus.CANCELLED:
        return CancelSummary(evaluation_run_id, 0, 0, already_cancelled=True)
    if run.status is EvaluationRunStatus.COMPLETED:
        raise OrchestrationError(f"实验 #{evaluation_run_id} 已经跑完了，没什么可取消的")

    in_flight = int(
        session.execute(
            sa.select(sa.func.count()).where(
                _job_filter(evaluation_run_id), JobQueue.state == JobState.LEASED
            )
        ).scalar_one()
    )
    result = session.execute(
        sa.update(JobQueue)
        .where(_job_filter(evaluation_run_id), JobQueue.state == JobState.PENDING)
        .values(
            state=JobState.DEAD,
            lease_owner=None,
            lease_expires_at=None,
            last_error=f"实验 #{evaluation_run_id} 已取消",
        )
        .execution_options(synchronize_session=False)
    )
    # `Session.execute()` 的静态类型是 `Result`（没有 rowcount），实际返回的是
    # `CursorResult`。和 `app.infrastructure.queue._changed_rows` 同一个坑
    dropped = cast("CursorResult[Any]", result).rowcount

    run.status = EvaluationRunStatus.CANCELLED
    run.finished_at = datetime.now(tz=UTC)
    logger.warning(
        "evaluation_run_cancelled",
        evaluation_run_id=evaluation_run_id,
        dropped_jobs=dropped,
        in_flight_jobs=in_flight,
    )
    return CancelSummary(evaluation_run_id, int(dropped), in_flight, already_cancelled=False)


# ── 补跑 ────────────────────────────────────────────────────


def retry_failed(session: Session, evaluation_run_id: int) -> RetrySummary:
    """把"没有认定结果、也没有在跑"的题补投一次作业。**不 commit**。

    补的是窟窿，不是重跑 —— 详见模块开头那段。三条边界：

    - 已经有 canonical attempt 的题不碰（C-25）；
    - attempt 数到了 4 次上限的题不补（C-71）；
    - 上一次已经拿到补丁的，带着那份补丁的 key 重投，由 `StoredPatchRunner`
      重放，**不再调用被测 AI**（C-54）。
    """
    run = session.execute(
        sa.select(EvaluationRun).where(EvaluationRun.id == evaluation_run_id).with_for_update()
    ).scalar_one_or_none()
    if run is None:
        raise OrchestrationError(f"找不到实验 #{evaluation_run_id}")
    if run.status is EvaluationRunStatus.COMPLETED:
        raise OrchestrationError(
            f"实验 #{evaluation_run_id} 每道题都有结论了，没有洞可补。"
            "要再跑一遍请新建一次实验（协议 C-55）"
        )

    attempts_by_task = _attempts_by_task(session, evaluation_run_id)
    live_tasks = _tasks_with_live_jobs(session, evaluation_run_id)
    all_tasks = _tasks_of_run(session, evaluation_run_id) | set(attempts_by_task)

    requeued: list[int] = []
    at_cap: list[int] = []
    decided = 0
    for task_id in sorted(all_tasks):
        attempts = attempts_by_task.get(task_id, [])
        if any(a.is_canonical for a in attempts):
            decided += 1
            continue
        if task_id in live_tasks:
            continue
        if len(attempts) >= MAX_ATTEMPTS_PER_TASK:
            at_cap.append(task_id)
            continue
        _requeue_task(session, run, task_id, attempts)
        requeued.append(task_id)

    if requeued and run.status is EvaluationRunStatus.CANCELLED:
        # 取消之后又决定接着跑：状态要回到 RUNNING，否则聚合会一直把它当已取消，
        # 补跑出来的结果永远不会被定案
        run.status = EvaluationRunStatus.RUNNING
        run.finished_at = None
    logger.info(
        "evaluation_run_retry_failed",
        evaluation_run_id=evaluation_run_id,
        requeued=len(requeued),
        at_attempt_cap=len(at_cap),
        already_decided=decided,
        still_running=len(live_tasks),
    )
    return RetrySummary(
        evaluation_run_id=evaluation_run_id,
        requeued=tuple(requeued),
        at_attempt_cap=tuple(at_cap),
        already_decided=decided,
        still_running=len(live_tasks),
    )


def _requeue_task(
    session: Session, run: EvaluationRun, task_id: int, attempts: Sequence[EvaluationTaskRun]
) -> None:
    """给一道题补投下一次 attempt。上次拿到过补丁就带上它的 key（C-54）。"""
    last = attempts[-1] if attempts else None
    reuse_key: str | None = None
    if last is not None and _has_normalized_patch(session, last.id):
        # key 是算出来的，不是从 patch_artifacts 读的 —— 那张表存的是物理 uri
        reuse_key = normalized_patch_key(
            evaluation_run_id=run.id,
            benchmark_task_id=task_id,
            attempt_no=last.attempt_no,
        )
    enqueue_eval_task(
        session,
        evaluation_run_id=run.id,
        benchmark_task_id=task_id,
        attempt_no=(last.attempt_no + 1) if last is not None else 1,
        retry_of_id=last.id if last is not None else None,
        reuse_patch_key=reuse_key,
        # 补跑优先于新题：一次实验里剩下的窟窿不填上，它就永远定不了案
        priority=1,
    )


def _has_normalized_patch(session: Session, task_run_id: int) -> bool:
    return (
        session.execute(
            sa.select(sa.func.count()).where(
                PatchArtifact.evaluation_task_run_id == task_run_id,
                PatchArtifact.kind == PatchKind.AGENT_NORMALIZED,
            )
        ).scalar_one()
        > 0
    )


# ── 兜底定案 ────────────────────────────────────────────────


def finalize_stale_runs(session: Session, *, limit: int = 20) -> list[int]:
    """把"已经没有活作业、但状态还停在 QUEUED/RUNNING"的实验定案。**不 commit**。

    正常路径是最后一道题跑完时顺手定案（在 `handle_eval_task` 的落库事务里）。
    这个兜底管的是最后一道题**没能正常收尾**的情况：处理函数抛异常、重试次数用完、
    作业被判 DEAD —— 那时没有任何人会去更新实验状态，它会永远停在 RUNNING，
    前端的进度条也就永远停在那儿。

    返回被定案的实验 id。
    """
    candidates = list(
        session.execute(
            sa.select(EvaluationRun.id)
            .where(
                EvaluationRun.status.in_([EvaluationRunStatus.QUEUED, EvaluationRunStatus.RUNNING])
            )
            .order_by(EvaluationRun.id)
            .limit(limit)
        ).scalars()
    )
    finalized: list[int] = []
    for run_id in candidates:
        if progress_mod.live_job_count(session, run_id, exclude_job_id=None) > 0:
            continue
        result = progress_mod.refresh(session, run_id)
        if result is not None and result.status is not EvaluationRunStatus.RUNNING:
            finalized.append(run_id)
    return finalized


# ── 查询小工具 ──────────────────────────────────────────────


def _job_filter(evaluation_run_id: int) -> sa.ColumnElement[bool]:
    """定位一次实验的 EVAL_TASK 作业。

    条件写在 payload 的 JSONB 上而不是一个外键列：`job_queue` 是**通用**队列
    （ADR-003 的风险栏要求"换 RQ 只改一个文件"），给它加评测专用的列，
    这条纪律就破了。这张表最多几千行，JSONB 上的等值过滤够快。
    """
    return sa.and_(
        JobQueue.job_type == JobType.EVAL_TASK,
        JobQueue.payload["evaluation_run_id"].astext == str(evaluation_run_id),
    )


def _tasks_of_run(session: Session, evaluation_run_id: int) -> set[int]:
    """这次实验覆盖哪些题 —— 从投过的作业里认。

    `evaluation_runs` 上只存了题数（`total_tasks`），没存题号清单。
    作业行不删（取消也只是标成 DEAD），所以它才是那份清单的落点。
    """
    rows = session.execute(
        sa.select(JobQueue.payload["benchmark_task_id"].astext).where(
            _job_filter(evaluation_run_id)
        )
    ).scalars()
    return {int(value) for value in rows if value is not None}


def _tasks_with_live_jobs(session: Session, evaluation_run_id: int) -> set[int]:
    rows = session.execute(
        sa.select(JobQueue.payload["benchmark_task_id"].astext).where(
            _job_filter(evaluation_run_id), JobQueue.state.in_(LIVE_JOB_STATES)
        )
    ).scalars()
    return {int(value) for value in rows if value is not None}


def _attempts_by_task(
    session: Session, evaluation_run_id: int
) -> dict[int, list[EvaluationTaskRun]]:
    """这次实验的全部 attempt，按题分组、组内按 attempt_no 排序。"""
    rows = session.execute(
        sa.select(EvaluationTaskRun)
        .where(EvaluationTaskRun.evaluation_run_id == evaluation_run_id)
        .order_by(EvaluationTaskRun.benchmark_task_id, EvaluationTaskRun.attempt_no)
    ).scalars()
    grouped: dict[int, list[EvaluationTaskRun]] = {}
    for row in rows:
        grouped.setdefault(row.benchmark_task_id, []).append(row)
    return grouped


def payload_of(job: JobQueue) -> EvalTaskPayload:
    """把一条作业的 payload 读成结构体。给取消看门线程和排查脚本用。"""
    return EvalTaskPayload.from_payload(job.payload)


def task_ids_from(jobs: Iterable[JobQueue]) -> list[int]:
    return [payload_of(job).benchmark_task_id for job in jobs]


__all__ = [
    "LIVE_JOB_STATES",
    "CancelSummary",
    "OrchestrationError",
    "RetrySummary",
    "cancel_run",
    "create_runs",
    "finalize_stale_runs",
    "mark_running",
    "payload_of",
    "retry_failed",
    "task_ids_from",
]
