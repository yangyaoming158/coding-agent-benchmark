"""Worker 主循环（E5-T1）。

    reap_orphans() ──▶ ┌── 回收过期租约 ──▶ 领一条 ──▶ 没有？等一会儿 ──┐
      （启动时一次）    │                      │                        │
                        │                      ▼                        │
                        │                 起心跳线程                    │
                        │                 跑处理函数（另一个线程）       │
                        │                 收尾 / 失败重排                │
                        └──────────────────────◀───────────────────────┘
                                   收到 SIGTERM 就跳出，再 reap_orphans()

## 三件事各自防的是什么

**回收过期租约** 防的是 Worker 被 `kill -9`。作业会一直挂在 `LEASED` 上，
从外面看像"还在跑"，实际上没人做。租约过期之后由任意一个 Worker 把它退回队列——
这就是验收标准里"杀死 Worker 后作业能被另一 Worker 接管"的实现。

**心跳线程** 防的是长作业被误判成僵尸。一次评测十几分钟，租约设 30 分钟，
每 60 秒续一次。用独立的 session 是因为 SQLAlchemy 的 `Session` 不是线程安全的，
不能借主线程那个。

**处理函数跑在另一个线程** 是为了让 SIGTERM 真的能生效。跑在主线程的话，
主线程就卡在处理函数里，`worker_shutdown_grace_s` 这个配置等于摆设——
而 docker daemon 偶尔会卡住，那时候唯一的出路就是 `kill -9`，
一 `kill -9` 就会留下残留容器，正好是验收标准要挡的那件事。

## 停机之后为什么还要再 reap 一次

处理函数正常收尾时，`run_in_container` 的 `finally` 会把容器删掉，通常不留东西。
但两种情况留得下来：等待超时被我们放弃的那个线程，和处理函数在删容器那一步自己崩了。
所以退出前无条件再扫一遍。

宽限期用完还去删容器，可能会删掉那个还在跑的线程正在用的容器。这是**故意的**：
进程马上就要退出了，那个线程也活不过进程，容器留下来只会一直占着内存和 pid，
让下一批评测因为资源不够而莫名其妙地失败——而失败原因会指向新任务，
不是这个已经死掉的 Worker。
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import traceback
from types import FrameType
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import JobState
from app.infrastructure import queue
from app.infrastructure.config import Settings, get_settings
from app.infrastructure.db import create_db_engine, create_session_factory, session_scope
from app.infrastructure.logging import get_logger
from app.infrastructure.models.job import JobQueue
from app.worker.registry import HandlerRegistry, JobContext

logger = get_logger(__name__)

#: `job_queue.lease_owner` 和 `evaluation_task_runs.worker_id` 都是 varchar(100)。
MAX_WORKER_ID_LENGTH = 100

#: 等处理函数线程时每次 join 多久。不一次性 join 到底是为了让主线程有机会处理信号——
#: Python 的信号处理器只在主线程的字节码之间跑。
_JOIN_TICK_S = 1.0


def default_worker_id() -> str:
    """没配 `WORKER_ID` 时用 `主机名-进程号`。

    进程号每次重启都变。一台机器上跑多个 Worker 时建议在配置里写死
    （worker-1、worker-2……）：启动时回收自己的残留容器要靠这个标识认领。
    """
    return f"{socket.gethostname()}-{os.getpid()}"[:MAX_WORKER_ID_LENGTH]


class _Result:
    """线程里跑处理函数的结果收集器。异常要带回主线程，不能只在线程里打印。"""

    __slots__ = ("error", "finished")

    def __init__(self) -> None:
        self.error: BaseException | None = None
        self.finished = False


class Worker:
    """一个 Worker 进程的全部行为。

    `run()` 是阻塞的主循环，`run_once()` 处理一条作业就返回——测试用后者，
    不用起线程也不用发信号就能把领取、心跳、收尾、失败重排全验一遍。
    """

    def __init__(
        self,
        registry: HandlerRegistry,
        *,
        settings: Settings | None = None,
        engine: Engine | None = None,
        session_factory: sessionmaker[Session] | None = None,
        docker_client: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.registry = registry
        self._engine = engine
        if session_factory is None:
            self._engine = engine or create_db_engine()
            session_factory = create_session_factory(self._engine)
        self.session_factory = session_factory
        self.worker_id = (self.settings.worker_id or default_worker_id())[:MAX_WORKER_ID_LENGTH]
        self._docker_client = docker_client
        self._stop = threading.Event()
        self._force = threading.Event()

    # ── 停机 ────────────────────────────────────────────────

    def request_stop(self, *, force: bool = False) -> None:
        """不再领新作业。`force` 为真时连当前作业也不等了。"""
        self._stop.set()
        if force:
            self._force.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def install_signal_handlers(self) -> None:
        """接管 SIGTERM 和 SIGINT。

        第一次：优雅停机——把手上这条做完，不再领新的。
        第二次：不等了，直接进收尾（还是会回收容器，不会裸奔退出）。
        """

        def handle(signum: int, _frame: FrameType | None) -> None:
            name = signal.Signals(signum).name
            if self._stop.is_set():
                logger.warning("worker_force_stop", signal=name)
                self._force.set()
            else:
                logger.info("worker_graceful_stop", signal=name, detail="做完手上这条就退出")
                self._stop.set()

        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)

    # ── 容器回收 ────────────────────────────────────────────

    def reap_orphan_containers(self) -> int:
        """删掉带 bench 标签的残留容器，返回删掉几个。

        Docker 用不了的时候只记一条警告就算了：没有 Docker 的环境（比如只跑
        落库类作业的 Worker）不该因为回收不了容器就起不来。
        """
        # 局部 import：`app.sandbox` 会去连 docker daemon，没有 Docker 的环境
        # 不该在 import Worker 的时候就炸
        from app.sandbox.container import reap_orphans

        try:
            removed = reap_orphans(
                client=self._docker_client, min_age_s=self.settings.worker_reap_min_age_s
            )
        except Exception as exc:  # SandboxError、docker SDK 的异常，都在这兜住
            logger.warning("reap_orphans_failed", error=f"{type(exc).__name__}: {exc}")
            return 0
        return len(removed)

    # ── 主循环 ──────────────────────────────────────────────

    def run(self) -> None:
        """阻塞式主循环，收到停机信号才返回。"""
        logger.info(
            "worker_started",
            worker_id=self.worker_id,
            job_types=[t.value for t in self.registry.job_types],
            lease_s=self.settings.job_lease_s,
            heartbeat_s=self.settings.job_heartbeat_s,
        )
        if self.settings.worker_reap_on_start:
            reaped = self.reap_orphan_containers()
            if reaped:
                logger.warning("startup_reaped_containers", count=reaped)

        try:
            while not self._stop.is_set():
                if not self.run_once():
                    # 队列空了，等一会儿再看。用 Event.wait 而不是 sleep：
                    # 收到停机信号能立刻醒过来，不用干等满一个轮询周期。
                    self._stop.wait(self.settings.job_poll_interval_s)
        finally:
            reaped = self.reap_orphan_containers()
            logger.info("worker_stopped", worker_id=self.worker_id, reaped_containers=reaped)

    def run_once(self) -> bool:
        """回收一遍僵尸、领一条作业跑掉。领不到返回 False。"""
        self._reap_expired_leases()
        job = self._lease()
        if job is None:
            return False
        self._process(job)
        return True

    # ── 内部步骤 ────────────────────────────────────────────

    def _reap_expired_leases(self) -> None:
        try:
            with session_scope(self.session_factory) as session:
                queue.reap_expired_leases(
                    session,
                    backoff_base_s=self.settings.job_retry_backoff_base_s,
                    backoff_cap_s=self.settings.job_retry_backoff_cap_s,
                )
        except Exception as exc:
            # 回收失败不该让 Worker 停摆：租约还在，下一轮再试就是了
            logger.warning("reap_expired_leases_failed", error=f"{type(exc).__name__}: {exc}")

    def _lease(self) -> JobQueue | None:
        """领一条作业，**单独一个事务并立刻提交**。

        必须马上提交：租约要让别的 Worker 看得见，不然它们会以为这条还没人领。

        返回的 ORM 对象在 session 关掉之后还能读字段，靠的是会话工厂建的时候设了
        `expire_on_commit=False`（见 `app.infrastructure.db.create_session_factory`）。
        默认行为下每读一个字段都会触发一次重新查询，而这时候 session 已经关了。
        """
        with session_scope(self.session_factory) as session:
            return queue.lease(
                session,
                worker_id=self.worker_id,
                job_types=self.registry.job_types,
                lease_s=self.settings.job_lease_s,
            )

    def _process(self, job: JobQueue) -> None:
        handler = self.registry.get(job.job_type)
        if handler is None:
            # 领取时已经按 job_types 过滤了，走到这里说明注册表在运行中被改了
            self._fail(job, f"没有 {job.job_type.value} 的处理函数")
            return

        ctx = JobContext(
            job_id=job.id,
            job_type=job.job_type,
            payload=job.payload,
            attempts=job.attempts,
            worker_id=self.worker_id,
            settings=self.settings,
            session_factory=self.session_factory,
            stop_event=self._stop,
        )

        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(job.id, heartbeat_stop, lease_lost),
            name=f"heartbeat-{job.id}",
            daemon=True,
        )
        heartbeat.start()

        result = _Result()
        try:
            self._run_handler(handler, ctx, result)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=_JOIN_TICK_S)

        if not result.finished:
            # 宽限期用完了，处理函数还在跑。不重排：它可能正跑到一半，
            # 让它的租约自然过期，由回收器按统一规则处理。
            logger.error("handler_abandoned", job_id=job.id, detail="停机宽限期用完，放弃等待")
            return

        if lease_lost.is_set():
            # 心跳期间租约被回收器判成僵尸交给了别人。这条作业现在不归我们，
            # 收尾和重排都会被 lease_owner 条件挡下来，写日志说清楚就行 ——
            # 接手的那个 Worker 已经在重跑了。
            logger.error(
                "job_abandoned_lease_lost",
                job_id=job.id,
                worker_id=self.worker_id,
                detail="租约在跑的过程中被回收，这次的结果丢弃",
            )
            return

        if result.error is not None:
            self._fail(job, f"{type(result.error).__name__}: {result.error}")
            return

        if not ctx.completed:
            # 处理函数没什么要落库的（或者忘了收尾）。补一次，别让作业挂在 LEASED 上。
            try:
                ctx.complete()
            except queue.LeaseLostError as exc:
                logger.warning("lease_lost_on_complete", job_id=job.id, error=str(exc))
                return
        logger.info("job_done", job_id=job.id, job_type=job.job_type.value)

    def _run_handler(self, handler: Any, ctx: JobContext, result: _Result) -> None:
        """在独立线程里跑处理函数，最多等 `worker_shutdown_grace_s`（只在停机时才有上限）。"""

        def target() -> None:
            try:
                handler(ctx)
            except BaseException as exc:  # 要原样带回主线程，包括 KeyboardInterrupt
                result.error = exc
            finally:
                result.finished = True

        thread = threading.Thread(target=target, name=f"job-{ctx.job_id}", daemon=True)
        thread.start()

        waited = 0.0
        while thread.is_alive():
            thread.join(timeout=_JOIN_TICK_S)
            if thread.is_alive() and self._stop.is_set():
                waited += _JOIN_TICK_S
                if self._force.is_set() or waited >= self.settings.worker_shutdown_grace_s:
                    return

    def _heartbeat(self, job_id: int, stop: threading.Event, lost: threading.Event) -> None:
        """每 `job_heartbeat_s` 秒把租约往后推一次，直到作业结束。"""
        while not stop.wait(self.settings.job_heartbeat_s):
            try:
                with session_scope(self.session_factory) as session:
                    queue.renew_lease(
                        session,
                        job_id=job_id,
                        worker_id=self.worker_id,
                        lease_s=self.settings.job_lease_s,
                    )
            except queue.LeaseLostError:
                # 已经被回收器判成僵尸交给别人了。这次的结果注定写不进去
                # （finish 带 lease_owner 条件），记一条日志让人能查到原因。
                logger.error("lease_lost", job_id=job_id, worker_id=self.worker_id)
                lost.set()
                return
            except Exception as exc:
                # 数据库抖一下不该让整条作业作废，下一拍再续。租约有 30 分钟，扛得住。
                logger.warning(
                    "heartbeat_failed", job_id=job_id, error=f"{type(exc).__name__}: {exc}"
                )

    def _fail(self, job: JobQueue, message: str) -> None:
        """处理函数抛异常了：还有次数就按退避重排，没有就标 FAILED。

        注意这里的次数是 `job_queue.max_attempts`，管的是"平台自己出问题"。
        评测层面的重试次数由协议 C-18 的映射表决定，走的是另投一条作业那条路。
        """
        logger.error(
            "job_handler_failed",
            job_id=job.id,
            job_type=job.job_type.value,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            error=message,
            tb=traceback.format_exc()[-2000:],
        )
        try:
            with session_scope(self.session_factory) as session:
                if job.attempts < job.max_attempts:
                    delay = queue.backoff_seconds(
                        job.attempts,
                        self.settings.job_retry_backoff_base_s,
                        cap_s=self.settings.job_retry_backoff_cap_s,
                    )
                    queue.release(
                        session,
                        job_id=job.id,
                        worker_id=self.worker_id,
                        delay_s=delay,
                        last_error=message,
                    )
                else:
                    queue.finish(
                        session,
                        job_id=job.id,
                        worker_id=self.worker_id,
                        state=JobState.FAILED,
                        last_error=message,
                    )
        except queue.LeaseLostError as exc:
            # 租约已经被回收器交给别人了，那边会重跑，这里什么都不用做
            logger.warning("lease_lost_on_fail", job_id=job.id, error=str(exc))


__all__ = ["MAX_WORKER_ID_LENGTH", "Worker", "default_worker_id"]
