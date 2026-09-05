"""Postgres 作业队列（E5-T1，ADR-003）。

一句话：**一张表 + `FOR UPDATE SKIP LOCKED`，就是全部的队列**。不引 Redis，
不引消息中间件（AGENTS.md §11 明确不做）。

## 这个文件不认识"评测"

它只认 `job_queue` 表：领一条、续租、做完、退回、回收僵尸。`payload` 是一个 JSON，
里面装什么由处理函数自己解释。

这条纪律是 ADR-003 的风险栏写死的：万一自研队列出现查不出原因的可靠性问题，
换成 RQ 只用改这一个文件，作业处理的代码一行不动。所以**禁止**在这里
import `app.evaluation` / `app.worker` 里的任何东西。

## 时间一律用数据库的时钟

`lease_expires_at`、`available_at` 全部用 `now()` 在数据库端算，不用 Python 的
`datetime.now()`。原因：租约是不是过期由回收器判断，判据是 `lease_expires_at < now()`，
这里的 `now()` 是数据库的。Worker 用自己的时钟写、数据库用自己的时钟读，
两边差几秒就会出现"租约还没到期就被回收"或者"过期了很久没人收"。

## 租约丢了就必须放弃这次的结果

`renew_lease()` 和 `finish()` 都带 `lease_owner = :worker_id` 这个条件，
改不到行就抛 `LeaseLostError`。

这不是防御性编程，是一个真会发生的场景：Worker 卡住超过租约时长（比如宿主机
换页换到假死），回收器把作业退回队列给了另一个 Worker，然后第一个 Worker 醒过来
接着写结果。不拦的话，同一道题会落两条 attempt 记录，成本被重复计一次。

拦下来的代价是第一个 Worker 那十几分钟白跑了。这个代价必须付：另一个 Worker
已经在重跑了，两份结果里我们没有任何依据挑出"对的那份"。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.expression import Executable

from app.domain.enums import JobState, JobType
from app.infrastructure.logging import get_logger
from app.infrastructure.models.job import JobQueue

logger = get_logger(__name__)

#: 退避上限（秒）。`2^attempts × base` 涨得很快，attempts 要是被别的路径写大了，
#: 一条作业可能被推到几天之后，看起来就像"作业丢了"。
DEFAULT_BACKOFF_CAP_S = 3600


class LeaseLostError(RuntimeError):
    """续租或收尾时发现作业已经不归自己了。

    调用方**必须**让事务回滚，不能吞掉它接着写结果——见模块开头那段。
    """

    def __init__(self, job_id: int, worker_id: str) -> None:
        super().__init__(f"作业 {job_id} 的租约已不属于 {worker_id}（超时被回收，或已被别人领走）")
        self.job_id = job_id
        self.worker_id = worker_id


@dataclass(frozen=True, slots=True)
class ReapResult:
    """一次僵尸回收的结果。两个列表装的都是 `job_queue.id`。"""

    #: 还有重试次数，已退回 PENDING（带退避）。
    requeued: tuple[int, ...] = ()
    #: 重试次数用完了，标成 DEAD 等人来看。
    dead: tuple[int, ...] = ()

    @property
    def total(self) -> int:
        return len(self.requeued) + len(self.dead)


def backoff_seconds(attempts: int, base_s: float, *, cap_s: float = DEFAULT_BACKOFF_CAP_S) -> float:
    """重试退避：`2^attempts × base`，封顶 `cap_s`（§15.2）。

    `attempts` 是**已经尝试过的次数**，所以第一次失败后等 `2×base`，
    第二次等 `4×base`。指数退避是为了让"外部服务在抖"这种故障有时间自己恢复，
    固定间隔重试只会在对方最脆弱的时候持续加压。
    """
    return min(base_s * float(2 ** max(attempts, 0)), cap_s)


def _changed_rows(session: Session, stmt: Executable) -> int:
    """执行一条 UPDATE，返回改到了几行。

    单独抽出来只为一件事：`Session.execute()` 的静态类型是 `Result`，没有
    `rowcount`；真正返回的是 `CursorResult`。在这里收口，省得每个调用点写一次
    `# type: ignore`——那种写法多了之后，真有类型错的地方也会被顺手忽略掉。
    """
    result = session.execute(stmt)
    return cast("CursorResult[Any]", result).rowcount


def _db_now_plus(seconds: float) -> ColumnElement[datetime]:
    """数据库时钟的 `now() + N 秒`。

    用 `CAST('N seconds' AS INTERVAL)` 而不是 Python 端算好一个绝对时间，
    理由见模块开头"时间一律用数据库的时钟"。
    """
    return sa.func.now() + sa.cast(sa.literal(f"{seconds:.3f} seconds"), sa.Interval)


def enqueue(
    session: Session,
    *,
    job_type: JobType,
    payload: dict[str, object],
    priority: int = 0,
    max_attempts: int = 3,
    delay_s: float = 0.0,
) -> JobQueue:
    """投一条作业。**不 commit** —— 事务边界归调用方。

    不 commit 是刻意的：投作业常常要和别的写操作绑在同一个事务里。最典型的是
    评测重试——"把这次的结果落库"和"排下一次 attempt"必须一起成功，
    分开提交会出现"结果写了但没人接着重试"。
    """
    job = JobQueue(
        job_type=job_type,
        payload=payload,
        priority=priority,
        state=JobState.PENDING,
        attempts=0,
        max_attempts=max_attempts,
        available_at=_db_now_plus(delay_s) if delay_s > 0 else sa.func.now(),
    )
    session.add(job)
    session.flush()
    return job


def lease(
    session: Session,
    *,
    worker_id: str,
    job_types: Sequence[JobType],
    lease_s: float,
) -> JobQueue | None:
    """领一条作业，没有就返回 None。

    就是 `07-platform-architecture.md` §15.2 的那条 SQL：

        UPDATE job_queue SET state='LEASED', ... WHERE id = (
            SELECT id FROM job_queue
            WHERE state='PENDING' AND available_at <= now()
            ORDER BY priority DESC, id ASC
            FOR UPDATE SKIP LOCKED LIMIT 1)

    `SKIP LOCKED` 是多 Worker 能并存的全部原因：它让第二个 Worker 直接跳过
    正在被别人锁住的那一行，而不是排队等锁。没有它，N 个 Worker 会在同一行上
    排成一队，并发退化成串行。

    `ORDER BY priority DESC, id ASC` 和 `ix_job_queue_pick` 索引的列序一致。
    """
    if not job_types:
        return None

    picked = (
        sa.select(JobQueue.id)
        .where(
            JobQueue.state == JobState.PENDING,
            JobQueue.available_at <= sa.func.now(),
            JobQueue.job_type.in_(list(job_types)),
        )
        .order_by(JobQueue.priority.desc(), JobQueue.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
        .scalar_subquery()
    )
    stmt = (
        sa.update(JobQueue)
        .where(JobQueue.id == picked)
        .values(
            state=JobState.LEASED,
            lease_owner=worker_id,
            lease_expires_at=_db_now_plus(lease_s),
            attempts=JobQueue.attempts + 1,
        )
        .returning(JobQueue)
        # populate_existing 不能省：这条作业可能已经在当前 session 的身份映射里
        # （比如刚投完就领），不加的话 RETURNING 回来的是那份**旧**的属性，
        # state 还写着 PENDING、attempts 还是 0，而数据库里其实已经改了。
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    job = session.execute(stmt).scalars().one_or_none()
    if job is not None:
        logger.info(
            "job_leased",
            job_id=job.id,
            job_type=job.job_type.value,
            attempts=job.attempts,
            worker_id=worker_id,
        )
    return job


def renew_lease(session: Session, *, job_id: int, worker_id: str, lease_s: float) -> None:
    """心跳续租。租约已经不归自己了就抛 `LeaseLostError`。

    每 `job_heartbeat_s` 秒一次，由 Worker 的心跳线程调用，用它自己的 session
    （SQLAlchemy 的 Session 不是线程安全的，主线程那个不能借给它）。
    """
    stmt = (
        sa.update(JobQueue)
        .where(
            JobQueue.id == job_id,
            JobQueue.state == JobState.LEASED,
            JobQueue.lease_owner == worker_id,
        )
        .values(lease_expires_at=_db_now_plus(lease_s))
        .execution_options(synchronize_session=False)
    )
    if _changed_rows(session, stmt) == 0:
        raise LeaseLostError(job_id, worker_id)


def finish(
    session: Session,
    *,
    job_id: int,
    worker_id: str,
    state: JobState = JobState.DONE,
    last_error: str | None = None,
) -> None:
    """收尾：把作业标成 DONE / FAILED / DEAD。**不 commit**。

    ⚠️ **"跑出 `ENV_BUILD_FAILED` 这种坏结果"也是 DONE。** 作业只关心"处理函数
    有没有正常返回"，不关心评测结论好不好看。评测层面的重试是**另投一条作业**
    （新的 `attempt_no`），不是把这条作业重来 —— 协议 C-32 要求重试新建记录，
    C-53 要求重试次数由 C-18 的表决定，而这张表的 `max_attempts` 和它对不上。

    这里用 FAILED 的只有一种情况：处理函数抛了没预料到的异常，而且重试次数用完了。
    """
    stmt = (
        sa.update(JobQueue)
        .where(
            JobQueue.id == job_id,
            JobQueue.state == JobState.LEASED,
            JobQueue.lease_owner == worker_id,
        )
        .values(
            state=state,
            lease_owner=None,
            lease_expires_at=None,
            last_error=last_error[:2000] if last_error else None,
        )
        .execution_options(synchronize_session=False)
    )
    if _changed_rows(session, stmt) == 0:
        raise LeaseLostError(job_id, worker_id)


def release(
    session: Session,
    *,
    job_id: int,
    worker_id: str,
    delay_s: float = 0.0,
    last_error: str | None = None,
) -> None:
    """把作业退回 PENDING，让别人（或者自己重启后）接着领。**不 commit**。

    两处用它：

    - 处理函数抛了异常，但 `attempts < max_attempts`，按退避重排；
    - 优雅停机时手上还攥着一条没开跑的作业，立刻还回去，不用等租约自然过期。
      少等的这一段就是 §15.2 说的"释放租约"，否则接手的 Worker 要干等 30 分钟。
    """
    stmt = (
        sa.update(JobQueue)
        .where(
            JobQueue.id == job_id,
            JobQueue.state == JobState.LEASED,
            JobQueue.lease_owner == worker_id,
        )
        .values(
            state=JobState.PENDING,
            lease_owner=None,
            lease_expires_at=None,
            available_at=_db_now_plus(delay_s) if delay_s > 0 else sa.func.now(),
            last_error=last_error[:2000] if last_error else None,
        )
        .execution_options(synchronize_session=False)
    )
    if _changed_rows(session, stmt) == 0:
        raise LeaseLostError(job_id, worker_id)


def reap_expired_leases(
    session: Session,
    *,
    backoff_base_s: float,
    backoff_cap_s: float = DEFAULT_BACKOFF_CAP_S,
    limit: int = 100,
) -> ReapResult:
    """僵尸回收：租约过期还没做完的，退回队列或者标成 DEAD。**不 commit**。

    这是"杀死 Worker 后作业能被另一 Worker 接管"的落点。Worker 被 `kill -9`
    的时候没有任何机会做收尾，那条作业会一直挂在 LEASED 上——不回收就永远没人做，
    而且从外面看它像是"还在跑"。

    走两步（先 `SELECT ... FOR UPDATE SKIP LOCKED` 再逐条 UPDATE）而不是一条大
    UPDATE，是因为退避时长要按每条自己的 `attempts` 算。同时过期的作业通常只有
    一两条，多几次往返不值得为它写一条难读的 SQL。`SKIP LOCKED` 保证多个 Worker
    同时回收也不会互相阻塞。
    """
    expired = (
        session.execute(
            sa.select(JobQueue.id, JobQueue.attempts, JobQueue.max_attempts)
            .where(
                JobQueue.state == JobState.LEASED,
                JobQueue.lease_expires_at < sa.func.now(),
            )
            .order_by(JobQueue.id.asc())
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
        .mappings()
        .all()
    )

    requeued: list[int] = []
    dead: list[int] = []
    for row in expired:
        job_id, attempts, max_attempts = row["id"], row["attempts"], row["max_attempts"]
        can_retry = attempts < max_attempts
        values: dict[str, object] = {
            "lease_owner": None,
            "lease_expires_at": None,
            "last_error": f"租约过期被回收（已尝试 {attempts}/{max_attempts} 次）",
        }
        if can_retry:
            delay = backoff_seconds(attempts, backoff_base_s, cap_s=backoff_cap_s)
            values["state"] = JobState.PENDING
            values["available_at"] = _db_now_plus(delay)
            requeued.append(job_id)
        else:
            values["state"] = JobState.DEAD
            dead.append(job_id)
        session.execute(
            sa.update(JobQueue)
            .where(JobQueue.id == job_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )

    if requeued or dead:
        logger.warning("reaped_expired_leases", requeued=len(requeued), dead=len(dead))
    return ReapResult(requeued=tuple(requeued), dead=tuple(dead))


__all__ = [
    "DEFAULT_BACKOFF_CAP_S",
    "LeaseLostError",
    "ReapResult",
    "backoff_seconds",
    "enqueue",
    "finish",
    "lease",
    "reap_expired_leases",
    "release",
    "renew_lease",
]
