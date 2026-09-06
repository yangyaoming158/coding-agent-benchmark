"""多槽位 Worker：并发领取、双层信号量、取消（E5-T2）。

**要真数据库，不要 Docker。** 处理函数是假的（记账 + 睡一会儿）：这一层测的是调度，
真评测那条路在 `tests/sandbox/test_worker_eval_task.py`。

三条验收标准里的两条落在这个文件：

- "8 并发"：槽位数确实能让 N 条作业同时在途（`test_slots_run_jobs_in_parallel`）；
- "取消能在 30 秒内停住"：这里用 0.1 秒的轮询把同一条路径压缩到秒级
  （`test_cancelling_a_run_stops_a_job_in_flight`）。真机上的实测另附。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import EvaluationRunStatus, JobState, JobType
from app.evaluation.gate import TaskCancelledError
from app.evaluation.orchestrator import cancel_run, create_runs
from app.infrastructure.config import Settings, get_settings
from app.infrastructure.db import create_session_factory
from app.infrastructure.models.evaluation import EvaluationRun
from app.infrastructure.models.job import JobQueue
from app.worker.concurrency import ConcurrencyLimits
from app.worker.loop import Worker
from app.worker.registry import HandlerRegistry, JobContext
from tests.integration.factories import Seeded, seed_minimal, wipe

pytestmark = pytest.mark.db

#: 等一件事发生的上限（秒）。到点还没发生就让测试失败，不要无限等。
WAIT_S = 20.0


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


class Peak:
    """记录同时进入某一段的最大人数。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.now = 0
        self.peak = 0

    def enter(self) -> None:
        with self._lock:
            self.now += 1
            self.peak = max(self.peak, self.now)

    def leave(self) -> None:
        with self._lock:
            self.now -= 1


@pytest.fixture
def factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        wipe(session)
    try:
        yield session_factory
    finally:
        with session_factory() as session:
            wipe(session)


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(
        update={
            "worker_id": "slots-worker",
            "job_poll_interval_s": 0.05,
            "job_heartbeat_s": 30,
            "job_lease_s": 120,
            "job_max_attempts": 1,
            "worker_shutdown_grace_s": 20.0,
            "worker_reap_on_start": False,
            # 取消的响应上限就是这个数。生产默认 5 秒，这里压到 0.1 秒好让测试快
            "cancel_poll_s": 0.1,
            "run_sweep_interval_s": 0.2,
        }
    )


def enqueue(factory: sessionmaker[Session], count: int, **payload: object) -> list[int]:
    from app.infrastructure import queue

    with factory() as session:
        ids = [
            queue.enqueue(
                session,
                job_type=JobType.EVAL_TASK,
                payload={"n": index, **payload},
                max_attempts=1,
            ).id
            for index in range(count)
        ]
        session.commit()
        return ids


def wait_until(predicate, *, timeout: float = WAIT_S, tick: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(tick)
    return False


def run_worker(worker: Worker) -> threading.Thread:
    thread = threading.Thread(target=worker.run, name="worker-under-test", daemon=True)
    thread.start()
    return thread


def states(factory: sessionmaker[Session]) -> list[JobState]:
    with factory() as session:
        return list(session.execute(sa.select(JobQueue.state).order_by(JobQueue.id)).scalars())


# ── 槽位 ────────────────────────────────────────────────────


def test_slots_run_jobs_in_parallel(factory: sessionmaker[Session], settings: Settings) -> None:
    """3 个槽位 → 3 条作业同时在途，剩下的排队。

    这是"并行度 ≥ 8"的机制：对外声明的并行度就是同时在途的评测任务数（§4.6），
    而在途数由槽位数决定。E5-T1 的 Worker 是一次一条，这条测试在那时必然失败。
    """
    enqueue(factory, 6)
    peak = Peak()
    release = threading.Event()

    def handler(_ctx: JobContext) -> None:
        peak.enter()
        try:
            release.wait(timeout=WAIT_S)
        finally:
            peak.leave()

    worker = Worker(
        HandlerRegistry({JobType.EVAL_TASK: handler}),
        settings=settings,
        session_factory=factory,
        docker_client=FakeDocker(),
        slots=3,
    )
    thread = run_worker(worker)
    try:
        assert wait_until(lambda: peak.peak >= 3), f"槽位没跑满，峰值只到 {peak.peak}"
        assert peak.now <= 3, "在途数超过槽位数了"
        assert worker.in_flight <= 3
        release.set()
        # 放开之后剩下 3 条会被接着领走，槽位是循环用的
        assert wait_until(lambda: states(factory) == [JobState.DONE] * 6), states(factory)
        assert peak.peak == 3, f"槽位数是 3，却观察到 {peak.peak} 条同时在跑"
    finally:
        release.set()
        worker.request_stop()
        thread.join(timeout=WAIT_S)

    assert not thread.is_alive()


def test_the_two_semaphores_are_counted_separately(
    factory: sessionmaker[Session], settings: Settings
) -> None:
    """AI 那层放 2 个进去，沙箱那层只放 1 个 —— 两把信号量各管各的（ADR-012）。

    `barrier.wait()` 是关键：两个线程必须**同时**在 AI 阶段里，
    才可能一起过这道栅栏。合成一把信号量的话（沙箱只剩 1 个名额），
    第二个线程会被挡在外面，栅栏超时，测试失败。
    """
    enqueue(factory, 4)
    sandbox_peak = Peak()
    both_in_agent = threading.Barrier(2)
    agent_pairs = threading.Semaphore(0)

    def handler(ctx: JobContext) -> None:
        with ctx.gate.agent():
            try:
                both_in_agent.wait(timeout=WAIT_S)
                agent_pairs.release()
            except threading.BrokenBarrierError:
                pass
        with ctx.gate.sandbox():
            sandbox_peak.enter()
            time.sleep(0.05)
            sandbox_peak.leave()

    worker = Worker(
        HandlerRegistry({JobType.EVAL_TASK: handler}),
        settings=settings,
        session_factory=factory,
        docker_client=FakeDocker(),
        slots=4,
        limits=ConcurrencyLimits(agent=2, sandbox=1, poll_s=0.02),
    )
    thread = run_worker(worker)
    try:
        assert wait_until(lambda: states(factory) == [JobState.DONE] * 4), states(factory)
    finally:
        worker.request_stop()
        thread.join(timeout=WAIT_S)

    assert agent_pairs.acquire(blocking=False), "两个 AI 阶段从来没有同时跑过"
    assert sandbox_peak.peak == 1, f"沙箱名额只有 1 个，却观察到 {sandbox_peak.peak} 个同时在跑"


def test_a_freed_slot_is_refilled_without_waiting_for_the_next_poll(
    factory: sessionmaker[Session], settings: Settings
) -> None:
    """一个槽空出来，下一条作业要立刻补上，不能等到下一次轮询。

    这是一条**回归测试**。2026-09-06 的 120 条作业实测里，槽满的时候主循环
    干等了一整个轮询周期（5 秒）：一批题一秒跑完，机器接着空转四秒。
    峰值看起来还是满的 8，但有效并发的 P50 是 0 —— 单看峰值发现不了，
    这正是验收标准要"时间序列"而不是一个峰值数字的原因。

    轮询周期在这里设成 5 秒（生产默认值）：修好之前这条测试要跑 5 秒以上，
    修好之后不到 1 秒。
    """
    slow_poll = settings.model_copy(update={"job_poll_interval_s": 5.0})
    enqueue(factory, 4)
    done = threading.Event()

    def handler(_ctx: JobContext) -> None:
        return None

    worker = Worker(
        HandlerRegistry({JobType.EVAL_TASK: handler}),
        settings=slow_poll,
        session_factory=factory,
        docker_client=FakeDocker(),
        slots=1,  # 一次只跑一条，四条作业要串成四轮
    )
    began = time.monotonic()
    thread = run_worker(worker)
    try:
        assert wait_until(lambda: states(factory) == [JobState.DONE] * 4, timeout=WAIT_S)
        done.set()
    finally:
        worker.request_stop()
        thread.join(timeout=WAIT_S)

    elapsed = time.monotonic() - began
    assert done.is_set()
    assert elapsed < 5.0, f"四条作业花了 {elapsed:.1f} 秒，槽位空出来之后没有立刻补上"


# ── 取消 ────────────────────────────────────────────────────


@pytest.fixture
def seeded_run(factory: sessionmaker[Session]) -> tuple[Seeded, int]:
    with factory() as session:
        seeded = seed_minimal(session, tasks=1)
        (run,) = create_runs(
            session,
            name="取消测试",
            benchmark_set_id=seeded.benchmark_set_id,
            agent_config_id=seeded.agent_config_id,
            task_ids=seeded.task_ids,
            agent_concurrency=2,
            sandbox_concurrency=1,
        )
        session.commit()
        return seeded, run.id


def test_cancelling_a_run_stops_a_job_in_flight(
    factory: sessionmaker[Session], settings: Settings, seeded_run: tuple[Seeded, int]
) -> None:
    """取消一次实验 → 正在跑的那条作业在一个轮询周期内收到取消信号。

    验的是"取消能在 30 秒内停住"那条验收标准的机制部分：看门线程发现实验状态变了，
    置取消标志，正卡在阶段闸门上的执行立刻抛 `TaskCancelledError`。
    真机上还会顺手把容器杀掉，那部分要 Docker，在 `tests/sandbox/` 里。
    """
    _, run_id = seeded_run
    started = threading.Event()
    observed: list[float] = []

    def handler(ctx: JobContext) -> None:
        started.set()
        began = time.monotonic()
        # 模拟"跑了很久的一步"：真实路径上这里是等容器
        deadline = began + WAIT_S
        while time.monotonic() < deadline:
            try:
                ctx.gate.raise_if_cancelled()
            except TaskCancelledError:
                observed.append(time.monotonic() - began)
                return
            time.sleep(0.02)

    worker = Worker(
        HandlerRegistry({JobType.EVAL_TASK: handler}),
        settings=settings,
        session_factory=factory,
        docker_client=FakeDocker(),
        slots=2,
    )
    thread = run_worker(worker)
    try:
        assert started.wait(timeout=WAIT_S), "作业没被领走"
        with factory() as session:
            summary = cancel_run(session, run_id)
            session.commit()
        assert summary.in_flight_jobs == 1
        assert wait_until(lambda: bool(observed)), "取消信号没传到正在跑的作业上"
    finally:
        worker.request_stop()
        thread.join(timeout=WAIT_S)

    assert observed[0] < 5.0, f"取消之后 {observed[0]:.1f} 秒才被发现，太慢了"
    with factory() as session:
        run = session.get(EvaluationRun, run_id)
        assert run is not None
        assert run.status is EvaluationRunStatus.CANCELLED


def test_a_stopping_worker_waits_for_jobs_in_flight(
    factory: sessionmaker[Session], settings: Settings
) -> None:
    """收到停机信号时手上还有活 → 等它做完再退，作业不会被丢在 LEASED 上。

    丢在 LEASED 上的话，下一个 Worker 要等租约自然过期（默认 30 分钟）才能接手。
    """
    enqueue(factory, 2)
    entered = threading.Event()
    release = threading.Event()

    def handler(_ctx: JobContext) -> None:
        entered.set()
        release.wait(timeout=WAIT_S)

    worker = Worker(
        HandlerRegistry({JobType.EVAL_TASK: handler}),
        settings=settings,
        session_factory=factory,
        docker_client=FakeDocker(),
        slots=2,
    )
    thread = run_worker(worker)
    try:
        assert entered.wait(timeout=WAIT_S)
        worker.request_stop()
        time.sleep(0.2)
        assert thread.is_alive(), "手上还有活就退出了"
        release.set()
        thread.join(timeout=WAIT_S)
    finally:
        release.set()
        worker.request_stop(force=True)
        thread.join(timeout=WAIT_S)

    assert not thread.is_alive()
    assert states(factory) == [JobState.DONE, JobState.DONE]
