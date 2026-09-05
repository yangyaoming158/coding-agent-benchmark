"""Postgres 作业队列（E5-T1，`07-platform-architecture.md` §15.2）。

这一组必须连真数据库。`SKIP LOCKED`、`FOR UPDATE`、部分索引、`now()` 的语义
全是 Postgres 的行为，用 SQLite 或者假对象测等于什么都没测。

## 两种夹具，用途不同

- `session`：整个测试包在一个事务里，结束回滚。大部分用例用它，互不干扰。
- `committed`：真提交、真并发。只有"两个 Worker 同时抢一条"和"租约过期被接管"
  这两类要用——它们要验的正是**跨连接**的可见性，回滚型夹具看不见对方的数据。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import JobState, JobType
from app.infrastructure import queue
from app.infrastructure.db import create_session_factory
from app.infrastructure.models.job import JobQueue

pytestmark = pytest.mark.db

WORKER_A = "worker-a"
WORKER_B = "worker-b"


@pytest.fixture
def committed(engine: Engine) -> Iterator[sessionmaker[Session]]:
    """会真提交的会话工厂。用完把 `job_queue` 清空。

    清空而不是回滚：这些用例要跨连接看见彼此的数据，就不能都待在同一个
    未提交的事务里。代价是收尾要自己做。
    """
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        with factory() as session:
            session.execute(sa.delete(JobQueue))
            session.commit()


def put(session: Session, **kwargs: object) -> JobQueue:
    """投一条 EVAL_TASK 作业，payload 随便填。"""
    defaults: dict[str, object] = {"job_type": JobType.EVAL_TASK, "payload": {"n": 1}}
    return queue.enqueue(session, **{**defaults, **kwargs})  # type: ignore[arg-type]


# ── 领取 ────────────────────────────────────────────────────


def test_lease_marks_the_job_and_counts_the_attempt(session: Session) -> None:
    """领走之后：状态 LEASED、归属写上、租约有到期时间、尝试次数加一。

    `attempts` 在**领取时**加，不是在失败时加。这样 Worker 被 kill 掉、
    连收尾都没机会做的时候，这次尝试也算数——否则一个每次都把 Worker 搞崩的
    作业会被无限重试。
    """
    job = put(session)
    assert job.attempts == 0

    leased = queue.lease(session, worker_id=WORKER_A, job_types=[JobType.EVAL_TASK], lease_s=60)

    assert leased is not None
    assert leased.id == job.id
    assert leased.state is JobState.LEASED
    assert leased.lease_owner == WORKER_A
    assert leased.lease_expires_at is not None
    assert leased.attempts == 1


def test_empty_queue_returns_none(session: Session) -> None:
    assert (
        queue.lease(session, worker_id=WORKER_A, job_types=[JobType.EVAL_TASK], lease_s=60) is None
    )


def test_delayed_job_is_not_visible_yet(session: Session) -> None:
    """`available_at` 还没到的作业领不走 —— 退避就是靠这个字段实现的。"""
    put(session, delay_s=3600)
    assert (
        queue.lease(session, worker_id=WORKER_A, job_types=[JobType.EVAL_TASK], lease_s=60) is None
    )


def test_higher_priority_goes_first(session: Session) -> None:
    """按 `priority DESC, id ASC` 排。重试作业投的 priority 是 1，会插到新题前面。

    为什么重试要优先：一道题拖着不结束，整次实验就结束不了。
    """
    put(session, priority=0)
    urgent = put(session, priority=5)
    put(session, priority=0)

    leased = queue.lease(session, worker_id=WORKER_A, job_types=[JobType.EVAL_TASK], lease_s=60)
    assert leased is not None and leased.id == urgent.id


def test_same_priority_is_first_in_first_out(session: Session) -> None:
    first = put(session)
    put(session)
    leased = queue.lease(session, worker_id=WORKER_A, job_types=[JobType.EVAL_TASK], lease_s=60)
    assert leased is not None and leased.id == first.id


def test_only_registered_job_types_are_leased(session: Session) -> None:
    """不领自己处理不了的作业。

    不过滤的话，一个只会跑评测的 Worker 会把建镜像的作业领走然后失败，
    那条作业的重试次数就这么被白白耗光了。
    """
    put(session, job_type=JobType.BUILD_IMAGE)
    assert (
        queue.lease(session, worker_id=WORKER_A, job_types=[JobType.EVAL_TASK], lease_s=60) is None
    )


def test_no_job_types_leases_nothing(session: Session) -> None:
    """注册表是空的就什么都不领，而不是把所有类型都领走。"""
    put(session)
    assert queue.lease(session, worker_id=WORKER_A, job_types=[], lease_s=60) is None


def test_two_workers_never_get_the_same_job(committed: sessionmaker[Session]) -> None:
    """`SKIP LOCKED`：并发领取时同一条作业只会落到一个 Worker 手里。

    这是多 Worker 能并存的**全部**依据。没有它，两个 Worker 会同时跑同一道题，
    落两条 attempt 记录、烧两份钱、还会在 canonical 标记上撞唯一索引。

    造法：投 6 条，10 个线程一起抢，每人抢一条。所有抢到的 id 必须两两不同。
    """
    with committed() as session:
        for _ in range(6):
            put(session)
        session.commit()

    got: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(10)

    def grab(worker_no: int) -> None:
        barrier.wait()  # 尽量让 10 个线程真的挤在同一时刻
        with committed() as session:
            job = queue.lease(
                session, worker_id=f"w{worker_no}", job_types=[JobType.EVAL_TASK], lease_s=60
            )
            session.commit()
        if job is not None:
            with lock:
                got.append(job.id)

    threads = [threading.Thread(target=grab, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(got) == 6, f"6 条作业应该全被领走，实际领走 {len(got)} 条"
    assert len(set(got)) == len(got), f"同一条作业被领了多次：{got}"


# ── 续租与收尾：租约归属必须校验 ─────────────────────────────


def test_renew_pushes_the_deadline_out(session: Session) -> None:
    job = put(session)
    leased = queue.lease(session, worker_id=WORKER_A, job_types=[JobType.EVAL_TASK], lease_s=1)
    assert leased is not None
    before = leased.lease_expires_at

    queue.renew_lease(session, job_id=job.id, worker_id=WORKER_A, lease_s=3600)
    session.refresh(leased)

    assert before is not None and leased.lease_expires_at is not None
    assert leased.lease_expires_at > before


def test_renew_by_a_stranger_is_refused(session: Session) -> None:
    """租约不归自己就不许续。

    这是个真会发生的场景：Worker 卡住超过租约时长，作业被回收器交给了别人，
    然后它醒过来接着续租。让它续成功的话，两个 Worker 会同时认为这条归自己。
    """
    job = put(session)
    queue.lease(session, worker_id=WORKER_A, job_types=[JobType.EVAL_TASK], lease_s=60)

    with pytest.raises(queue.LeaseLostError):
        queue.renew_lease(session, job_id=job.id, worker_id=WORKER_B, lease_s=60)


def test_finish_by_a_stranger_is_refused(session: Session) -> None:
    """租约丢了就写不进结果 —— 这是"不许写重复 attempt"的最后一道闸。"""
    job = put(session)
    queue.lease(session, worker_id=WORKER_A, job_types=[JobType.EVAL_TASK], lease_s=60)

    with pytest.raises(queue.LeaseLostError):
        queue.finish(session, job_id=job.id, worker_id=WORKER_B)


def test_finish_clears_the_lease(session: Session) -> None:
    job = put(session)
    queue.lease(session, worker_id=WORKER_A, job_types=[JobType.EVAL_TASK], lease_s=60)
    queue.finish(session, job_id=job.id, worker_id=WORKER_A)
    session.refresh(job)

    assert job.state is JobState.DONE
    assert job.lease_owner is None
    assert job.lease_expires_at is None


def test_release_puts_it_back_with_a_delay(session: Session) -> None:
    """退回队列 + 退避。退避期内不该被再次领走。"""
    job = put(session)
    queue.lease(session, worker_id=WORKER_A, job_types=[JobType.EVAL_TASK], lease_s=60)
    queue.release(session, job_id=job.id, worker_id=WORKER_A, delay_s=3600, last_error="炸了")
    session.refresh(job)

    assert job.state is JobState.PENDING
    assert job.lease_owner is None
    assert job.last_error == "炸了"
    assert (
        queue.lease(session, worker_id=WORKER_B, job_types=[JobType.EVAL_TASK], lease_s=60) is None
    )


# ── 僵尸回收 ────────────────────────────────────────────────


def test_expired_lease_goes_back_to_the_queue(session: Session) -> None:
    """租约过期 + 还有次数 → 退回 PENDING。

    这就是"杀死 Worker 后作业能被另一 Worker 接管"的机制。
    `lease_s` 传负数 = 一领到手就已经过期，省得在测试里等 30 分钟。
    """
    job = put(session, max_attempts=3)
    queue.lease(session, worker_id=WORKER_A, job_types=[JobType.EVAL_TASK], lease_s=-1)

    result = queue.reap_expired_leases(session, backoff_base_s=0.001)
    session.refresh(job)

    assert result.requeued == (job.id,)
    assert result.dead == ()
    assert job.state is JobState.PENDING
    assert job.lease_owner is None
    assert job.last_error is not None and "租约过期" in job.last_error


def test_expired_lease_with_no_attempts_left_is_dead(session: Session) -> None:
    """次数用完了就标 DEAD，不再无限重排。

    每次都把 Worker 搞崩的作业（比如 payload 里的题目 id 根本不存在）
    要停下来等人看，不能一直占着调度。
    """
    job = put(session, max_attempts=1)
    queue.lease(session, worker_id=WORKER_A, job_types=[JobType.EVAL_TASK], lease_s=-1)

    result = queue.reap_expired_leases(session, backoff_base_s=0.001)
    session.refresh(job)

    assert result.dead == (job.id,)
    assert job.state is JobState.DEAD


def test_healthy_leases_are_left_alone(session: Session) -> None:
    """还没过期的租约不能碰 —— 碰了就是把正在跑的作业抢走。"""
    job = put(session)
    queue.lease(session, worker_id=WORKER_A, job_types=[JobType.EVAL_TASK], lease_s=3600)

    assert queue.reap_expired_leases(session, backoff_base_s=1).total == 0
    session.refresh(job)
    assert job.state is JobState.LEASED


def test_reap_applies_backoff(session: Session) -> None:
    """回收之后要等退避时间才能再领，不是立刻可领。

    立刻可领的话，一个必然失败的作业会在几秒内把重试次数烧光，
    而"重试"的意义是给外部故障留出恢复时间。
    """
    put(session, max_attempts=3)
    queue.lease(session, worker_id=WORKER_A, job_types=[JobType.EVAL_TASK], lease_s=-1)
    queue.reap_expired_leases(session, backoff_base_s=3600)

    assert (
        queue.lease(session, worker_id=WORKER_B, job_types=[JobType.EVAL_TASK], lease_s=60) is None
    )


def test_another_worker_takes_over_after_a_kill(committed: sessionmaker[Session]) -> None:
    """**验收标准**：杀死 Worker 之后作业能被另一个 Worker 接管。

    模拟的就是 `kill -9`：A 领走作业，然后什么都没做（没收尾、没续租）。
    租约一过期，B 回收之后领得到，而且拿到的是同一条作业。
    """
    with committed() as session:
        job = put(session, max_attempts=3)
        job_id = job.id
        session.commit()

    # A 领走就"死"了 —— 租约设成已过期，等价于它再也不会续租
    with committed() as session:
        taken = queue.lease(session, worker_id=WORKER_A, job_types=[JobType.EVAL_TASK], lease_s=-1)
        session.commit()
    assert taken is not None and taken.id == job_id

    with committed() as session:
        assert queue.reap_expired_leases(session, backoff_base_s=0).requeued == (job_id,)
        session.commit()

    with committed() as session:
        recovered = queue.lease(
            session, worker_id=WORKER_B, job_types=[JobType.EVAL_TASK], lease_s=60
        )
        session.commit()

    assert recovered is not None
    assert recovered.id == job_id
    assert recovered.lease_owner == WORKER_B
    # 两次领取都算数：A 那次没白算，否则崩溃循环会被无限重试
    assert recovered.attempts == 2


# ── 退避算法 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [(0, 30.0), (1, 60.0), (2, 120.0), (3, 240.0)],
)
def test_backoff_doubles_each_time(attempts: int, expected: float) -> None:
    """`2^attempts × base`（§15.2）。"""
    assert queue.backoff_seconds(attempts, 30.0) == expected


def test_backoff_is_capped() -> None:
    """封顶。不封的话 `attempts` 稍微大一点，作业就被推到几天之后，
    从外面看就像"作业丢了"。"""
    assert queue.backoff_seconds(20, 30.0, cap_s=3600) == 3600
