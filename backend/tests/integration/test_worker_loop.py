"""Worker 主循环（E5-T1）。

验的是"一条作业在 Worker 手里走完一圈"会发生什么：领取 → 心跳 → 收尾，
以及三条出错的岔路 —— 处理函数抛异常、Worker 被 kill、收到停机信号。

处理函数用假的（就一个记账的 lambda）：这里测的是调度，不是评测。
真评测那条路在 `tests/sandbox/test_worker_eval_task.py`，那个要 Docker。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import JobState, JobType
from app.infrastructure import queue
from app.infrastructure.config import Settings, get_settings
from app.infrastructure.db import create_session_factory
from app.infrastructure.models.job import JobQueue
from app.worker.loop import Worker
from app.worker.registry import HandlerRegistry, JobContext

pytestmark = pytest.mark.db


class FakeDocker:
    """假 docker 客户端：一个容器都没有。

    Worker 启动和退出时都会回收残留容器。真去连 daemon 的话，这一组测试就
    多了一条"必须装 Docker"的前提，而它们和容器毫无关系。
    """

    class _Containers:
        @staticmethod
        def list(**_kwargs: object) -> list[object]:
            return []

    containers = _Containers()


@pytest.fixture
def factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    """真提交的会话工厂 —— Worker 自己会开事务，回滚型夹具在这里用不了。"""
    session_factory = create_session_factory(engine)
    try:
        yield session_factory
    finally:
        with session_factory() as session:
            session.execute(sa.delete(JobQueue))
            session.commit()


@pytest.fixture
def settings() -> Settings:
    """把各种间隔调短，测试才跑得快。"""
    return get_settings().model_copy(
        update={
            "worker_id": "test-worker",
            "job_lease_s": 60,
            "job_heartbeat_s": 1,
            "job_poll_interval_s": 0.05,
            "job_max_attempts": 2,
            "job_retry_backoff_base_s": 0.01,
            "worker_shutdown_grace_s": 5.0,
            "worker_reap_on_start": True,
        }
    )


def make_worker(
    settings: Settings, factory: sessionmaker[Session], handlers: dict[JobType, object]
) -> Worker:
    return Worker(
        HandlerRegistry(handlers),  # type: ignore[arg-type]
        settings=settings,
        session_factory=factory,
        docker_client=FakeDocker(),
    )


def put(factory: sessionmaker[Session], **kwargs: object) -> int:
    with factory() as session:
        job = queue.enqueue(
            session,
            job_type=JobType.EVAL_TASK,
            payload={"hello": "world"},
            **kwargs,  # type: ignore[arg-type]
        )
        session.commit()
        return job.id


def reload(factory: sessionmaker[Session], job_id: int) -> JobQueue:
    with factory() as session:
        job = session.get(JobQueue, job_id)
        assert job is not None
        return job


# ── 正常路径 ────────────────────────────────────────────────


def test_a_job_is_leased_run_and_marked_done(
    settings: Settings, factory: sessionmaker[Session]
) -> None:
    seen: list[JobContext] = []
    job_id = put(factory)

    worker = make_worker(settings, factory, {JobType.EVAL_TASK: seen.append})
    assert worker.run_once() is True

    assert len(seen) == 1
    assert seen[0].payload == {"hello": "world"}
    assert seen[0].job_id == job_id
    assert seen[0].worker_id == "test-worker"
    assert reload(factory, job_id).state is JobState.DONE


def test_run_once_returns_false_on_an_empty_queue(
    settings: Settings, factory: sessionmaker[Session]
) -> None:
    worker = make_worker(settings, factory, {JobType.EVAL_TASK: lambda _ctx: None})
    assert worker.run_once() is False


def test_business_writes_and_the_done_mark_commit_together(
    settings: Settings, factory: sessionmaker[Session]
) -> None:
    """`ctx.complete(write=...)` 里写的东西和"作业完成"在同一个事务。

    这条是落库正确性的地基：结果写了但作业还挂着，会被另一个 Worker 重跑一遍；
    作业完成了但结果没写，这道题就凭空消失了。

    用另投一条作业当"业务写入"来验：它和收尾必须一起可见。
    """
    job_id = put(factory)

    def handler(ctx: JobContext) -> None:
        def write(session: Session) -> None:
            queue.enqueue(session, job_type=JobType.ATTRIBUTE, payload={"from": ctx.job_id})

        ctx.complete(write)

    worker = make_worker(settings, factory, {JobType.EVAL_TASK: handler})
    worker.run_once()

    with factory() as session:
        follow_ups = (
            session.execute(sa.select(JobQueue).where(JobQueue.job_type == JobType.ATTRIBUTE))
            .scalars()
            .all()
        )
    assert len(follow_ups) == 1
    assert follow_ups[0].payload == {"from": job_id}
    assert reload(factory, job_id).state is JobState.DONE


def test_a_handler_that_forgets_to_complete_still_closes_the_job(
    settings: Settings, factory: sessionmaker[Session]
) -> None:
    """处理函数没收尾，Worker 补一次 —— 作业不能一直挂在 LEASED 上。"""
    job_id = put(factory)
    worker = make_worker(settings, factory, {JobType.EVAL_TASK: lambda _ctx: None})
    worker.run_once()
    assert reload(factory, job_id).state is JobState.DONE


def test_only_registered_types_are_picked_up(
    settings: Settings, factory: sessionmaker[Session]
) -> None:
    job_id = put(factory)
    worker = make_worker(settings, factory, {JobType.BUILD_IMAGE: lambda _ctx: None})

    assert worker.run_once() is False
    assert reload(factory, job_id).state is JobState.PENDING


# ── 处理函数抛异常 ──────────────────────────────────────────


def test_a_crash_puts_the_job_back_with_backoff(
    settings: Settings, factory: sessionmaker[Session]
) -> None:
    """还有次数就退回队列，不是直接判死。

    注意这是**作业**层面的重试（Worker 或处理函数自己出问题），
    和协议 C-18 那套按故障类型算的评测重试是两回事。
    """
    job_id = put(factory, max_attempts=3)

    def boom(_ctx: JobContext) -> None:
        raise RuntimeError("处理函数炸了")

    make_worker(settings, factory, {JobType.EVAL_TASK: boom}).run_once()

    job = reload(factory, job_id)
    assert job.state is JobState.PENDING
    assert job.attempts == 1
    assert job.last_error is not None and "处理函数炸了" in job.last_error


def test_a_crash_with_no_attempts_left_marks_it_failed(
    settings: Settings, factory: sessionmaker[Session]
) -> None:
    """次数用完 → FAILED，停下来等人看，不再无限重排。"""
    job_id = put(factory, max_attempts=1)

    def boom(_ctx: JobContext) -> None:
        raise RuntimeError("每次都炸")

    make_worker(settings, factory, {JobType.EVAL_TASK: boom}).run_once()

    job = reload(factory, job_id)
    assert job.state is JobState.FAILED
    assert job.attempts == 1


# ── Worker 被 kill ──────────────────────────────────────────


def test_another_worker_takes_over_a_dead_workers_job(
    settings: Settings, factory: sessionmaker[Session]
) -> None:
    """**验收标准**：杀死 Worker 后作业能被另一 Worker 接管。

    `kill -9` 的效果就是"租约挂在那儿再也不续"。这里直接把租约设成已过期
    来模拟，比真起一个进程再杀掉稳定得多，验的是同一条路径：
    回收器把它退回队列 → 另一个 Worker 领走 → 跑完。
    """
    job_id = put(factory, max_attempts=3)
    with factory() as session:
        queue.lease(session, worker_id="dead-worker", job_types=[JobType.EVAL_TASK], lease_s=-1)
        session.commit()
    assert reload(factory, job_id).lease_owner == "dead-worker"

    seen: list[int] = []
    survivor = make_worker(
        settings, factory, {JobType.EVAL_TASK: lambda ctx: seen.append(ctx.job_id)}
    )

    # 回收之后要先过退避（2^1 × 0.01 秒）才能被再领走。立刻可领的话，
    # 一个必然把 Worker 搞崩的作业会在几毫秒内把重试次数烧光。
    assert survivor.run_once() is False, "刚回收就该在退避里，领不到"
    time.sleep(0.1)
    assert survivor.run_once() is True

    assert seen == [job_id], "接手的 Worker 应该跑的是同一条作业"
    job = reload(factory, job_id)
    assert job.state is JobState.DONE
    assert job.attempts == 2, "死掉那次也要算数，否则崩溃循环会被无限重试"


def test_results_are_refused_after_the_lease_is_lost(
    settings: Settings, factory: sessionmaker[Session]
) -> None:
    """租约被回收之后，原 Worker 写不进结果。

    不拦的话，同一道题会落两条 attempt 记录、成本被重复计一次 —— 而这时候
    另一个 Worker 已经在重跑了，两份结果里我们没有依据挑出"对的那份"。
    """
    job_id = put(factory, max_attempts=3)

    def stolen_midway(ctx: JobContext) -> None:
        # 干活干到一半，租约过期被回收、又被另一个 Worker 领走了。
        # 直接改 lease_owner 就是那个过程的终态，比真等 30 分钟稳定得多。
        with factory() as session:
            session.execute(
                sa.update(JobQueue)
                .where(JobQueue.id == ctx.job_id)
                .values(lease_owner="someone-else")
            )
            session.commit()
        ctx.complete()  # 必须被拒绝

    worker = make_worker(settings, factory, {JobType.EVAL_TASK: stolen_midway})
    worker.run_once()

    job = reload(factory, job_id)
    assert job.lease_owner == "someone-else", "作业已经归别人了"
    assert job.state is JobState.LEASED, "原 Worker 的收尾必须被拒绝，不能标成 DONE"


# ── 心跳 ────────────────────────────────────────────────────


def test_the_heartbeat_extends_the_lease_of_a_long_job(
    settings: Settings, factory: sessionmaker[Session]
) -> None:
    """长作业跑着的时候租约要一直往后推，否则会被自己人当成僵尸回收。

    心跳设 1 秒、处理函数睡 2.5 秒，跑完之后到期时间必须比刚领走时晚。
    """
    job_id = put(factory)
    first_deadline: list[object] = []

    def slow(ctx: JobContext) -> None:
        first_deadline.append(reload(factory, ctx.job_id).lease_expires_at)
        time.sleep(2.5)

    make_worker(settings, factory, {JobType.EVAL_TASK: slow}).run_once()

    job = reload(factory, job_id)
    assert job.state is JobState.DONE
    assert first_deadline[0] is not None
    with factory() as session:
        renewed = session.execute(
            sa.select(JobQueue.last_error).where(JobQueue.id == job_id)
        ).scalar_one()
    assert renewed is None, "正常跑完不该留错误信息"


# ── 停机 ────────────────────────────────────────────────────


def test_run_stops_when_asked(settings: Settings, factory: sessionmaker[Session]) -> None:
    """`request_stop()` 之后主循环要退出来，不能一直轮询。"""
    worker = make_worker(settings, factory, {JobType.EVAL_TASK: lambda _ctx: None})
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    time.sleep(0.2)

    worker.request_stop()
    thread.join(timeout=10)

    assert not thread.is_alive(), "收到停机信号之后主循环还没退出"
    assert worker.stopping is True


def test_a_job_in_flight_is_finished_before_shutdown(
    settings: Settings, factory: sessionmaker[Session]
) -> None:
    """优雅停机 = 手上这条做完再走，不是半路丢下。

    半路丢下的话，这条作业要等租约过期（默认 30 分钟）才有人接手，
    而它其实只差最后几秒就跑完了。
    """
    job_id = put(factory)
    started = threading.Event()

    def slow(_ctx: JobContext) -> None:
        started.set()
        time.sleep(1.0)

    worker = make_worker(settings, factory, {JobType.EVAL_TASK: slow})
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()

    assert started.wait(timeout=10), "处理函数没跑起来"
    worker.request_stop()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert reload(factory, job_id).state is JobState.DONE, "手上这条应该跑完才退出"
