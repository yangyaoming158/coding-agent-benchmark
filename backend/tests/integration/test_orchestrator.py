"""实验编排：建、取消、补跑、兜底定案（E5-T2）。

**要真数据库，不要 Docker。** 这一层证明的是"排队和状态机对不对"，
不是"题跑得对不对"。

协议上最要紧的三条都在这里验：

- 取消之后**没被领走的作业**要立刻掐掉，正在跑的交给 Worker 自己停（C-56 之外的工程要求）；
- 补跑**只补没有结论的题**，已经有认定结果的一律不碰（C-25、C-55）；
- 一道题的 attempt 总数到 4 次就不再补（C-71）。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import (
    AgentOutcome,
    EvaluationRunStatus,
    InfraOutcome,
    JobState,
    LifecycleStatus,
    PatchKind,
)
from app.evaluation.orchestrator import (
    OrchestrationError,
    cancel_run,
    create_runs,
    finalize_stale_runs,
    mark_running,
    retry_failed,
)
from app.evaluation.progress import lock_run, refresh
from app.infrastructure.db import create_session_factory
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun, PatchArtifact
from app.infrastructure.models.job import JobQueue
from tests.integration.factories import Seeded, seed_minimal, wipe

pytestmark = pytest.mark.db


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
def seeded(factory: sessionmaker[Session]) -> Seeded:
    with factory() as session:
        result = seed_minimal(session, tasks=3)
        session.commit()
        return result


def make_runs(factory: sessionmaker[Session], seeded: Seeded, *, rounds: int = 1) -> list[int]:
    with factory() as session:
        runs = create_runs(
            session,
            name="测试",
            benchmark_set_id=seeded.benchmark_set_id,
            agent_config_id=seeded.agent_config_id,
            task_ids=seeded.task_ids,
            agent_concurrency=4,
            sandbox_concurrency=2,
            rounds=rounds,
        )
        session.commit()
        return [run.id for run in runs]


def jobs_of(factory: sessionmaker[Session], run_id: int) -> list[JobQueue]:
    with factory() as session:
        return list(
            session.execute(
                sa.select(JobQueue)
                .where(JobQueue.payload["evaluation_run_id"].astext == str(run_id))
                .order_by(JobQueue.id)
            ).scalars()
        )


def add_attempt(
    factory: sessionmaker[Session],
    run_id: int,
    task_id: int,
    *,
    attempt_no: int,
    canonical: bool,
    infra: InfraOutcome = InfraOutcome.SUCCESS,
    agent: AgentOutcome | None = AgentOutcome.RESOLVED,
    lifecycle: LifecycleStatus = LifecycleStatus.COMPLETED,
    with_patch: bool = False,
) -> int:
    """手工塞一条 attempt 记录。真跑一次要 Docker，这里只需要"库里有这么一行"。"""
    with factory() as session:
        row = EvaluationTaskRun(
            evaluation_run_id=run_id,
            benchmark_task_id=task_id,
            attempt_no=attempt_no,
            lifecycle_status=lifecycle,
            infra_outcome=infra,
            agent_outcome=agent,
            is_canonical=canonical,
            # C-69：NOT_ATTEMPTED 当且仅当 agent_started_at 为空，写反了会撞 CHECK 约束
            agent_started_at=(
                None if agent is AgentOutcome.NOT_ATTEMPTED else datetime.now(tz=UTC)
            ),
            prepare_started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC),
        )
        session.add(row)
        session.flush()
        if with_patch:
            session.add(
                PatchArtifact(
                    evaluation_task_run_id=row.id,
                    kind=PatchKind.AGENT_NORMALIZED,
                    uri="local://x/patch.diff.gz",
                    sha256="2" * 64,
                    size_bytes=10,
                    is_empty=False,
                )
            )
        session.commit()
        return row.id


# ── 建实验 ──────────────────────────────────────────────────


def test_creating_a_run_expands_every_task_into_a_job(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    """N 道题 → N 条作业，`total_tasks` 当场写死。

    `total_tasks` 是严格解决率的分母（C-21）。跑的过程中重算的话，
    一道题因为作业死了没留下记录，分母会跟着缩水，解决率反而变好看。
    """
    (run_id,) = make_runs(factory, seeded)
    with factory() as session:
        run = session.get(EvaluationRun, run_id)
        assert run is not None
        assert run.total_tasks == 3
        assert run.status is EvaluationRunStatus.QUEUED
        assert run.agent_concurrency == 4
        assert run.sandbox_concurrency == 2

    jobs = jobs_of(factory, run_id)
    assert len(jobs) == 3
    assert {int(str(j.payload["benchmark_task_id"])) for j in jobs} == set(seeded.task_ids)
    assert all(job.state is JobState.PENDING for job in jobs)


def test_rounds_create_separate_runs(factory: sessionmaker[Session], seeded: Seeded) -> None:
    """多轮取样 = 多个 `EvaluationRun`（协议 C-55），不是一个实验里跑两遍。

    C-57 的部分唯一索引限死了"每题至多一个认定结果"，
    同一个实验里跑两遍，第二遍的结论没有地方放。
    """
    run_ids = make_runs(factory, seeded, rounds=3)
    assert len(run_ids) == 3
    assert len(set(run_ids)) == 3
    for run_id in run_ids:
        assert len(jobs_of(factory, run_id)) == 3
    with factory() as session:
        names = list(
            session.execute(
                sa.select(EvaluationRun.name).where(EvaluationRun.id.in_(run_ids))
            ).scalars()
        )
    assert names == ["测试 · 第 1 轮", "测试 · 第 2 轮", "测试 · 第 3 轮"]


def test_creating_a_run_without_tasks_is_refused(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    with factory() as session, pytest.raises(OrchestrationError, match="一道题都没选中"):
        create_runs(
            session,
            name="空的",
            benchmark_set_id=seeded.benchmark_set_id,
            agent_config_id=seeded.agent_config_id,
            task_ids=[],
            agent_concurrency=1,
            sandbox_concurrency=1,
        )


def test_mark_running_only_moves_forward(factory: sessionmaker[Session], seeded: Seeded) -> None:
    """第一道题开跑时推到 RUNNING，`started_at` 只写第一次。

    每道题都覆盖一次的话，"这次实验什么时候开始的"会变成"最后一道题什么时候开始的"，
    makespan 就没法算了。
    """
    (run_id,) = make_runs(factory, seeded)
    with factory() as session:
        assert mark_running(session, run_id) is EvaluationRunStatus.RUNNING
        session.commit()
        first_started = session.get(EvaluationRun, run_id).started_at  # type: ignore[union-attr]

    with factory() as session:
        assert mark_running(session, run_id) is EvaluationRunStatus.RUNNING
        session.commit()
        assert session.get(EvaluationRun, run_id).started_at == first_started  # type: ignore[union-attr]


def test_mark_running_reports_a_cancelled_run(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    """作业在队列里躺着的时候实验被取消 → 处理函数拿到 CANCELLED，直接收手。

    没有这一下的话，取消之后被领走的作业还会老老实实起容器跑十几分钟。
    """
    (run_id,) = make_runs(factory, seeded)
    with factory() as session:
        cancel_run(session, run_id)
        session.commit()
    with factory() as session:
        assert mark_running(session, run_id) is EvaluationRunStatus.CANCELLED


# ── 取消 ────────────────────────────────────────────────────


def test_cancel_kills_pending_jobs_and_counts_the_running_ones(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    """没被领走的当场掐掉；已经在跑的只报数，由各自的 Worker 停。

    标成 DEAD 而不是删掉：留着才能事后回答"取消的时候还剩多少题没跑"。
    """
    (run_id,) = make_runs(factory, seeded)
    leased = jobs_of(factory, run_id)[0]
    with factory() as session:
        session.execute(
            sa.update(JobQueue)
            .where(JobQueue.id == leased.id)
            .values(state=JobState.LEASED, lease_owner="w1")
        )
        session.commit()

    with factory() as session:
        summary = cancel_run(session, run_id)
        session.commit()

    assert summary.dropped_jobs == 2
    assert summary.in_flight_jobs == 1
    assert summary.already_cancelled is False

    states = {job.id: job.state for job in jobs_of(factory, run_id)}
    assert states.pop(leased.id) is JobState.LEASED, "在跑的那条不动，Worker 自己会收尾"
    assert set(states.values()) == {JobState.DEAD}
    with factory() as session:
        run = session.get(EvaluationRun, run_id)
        assert run is not None
        assert run.status is EvaluationRunStatus.CANCELLED
        assert run.finished_at is not None


def test_cancelling_twice_is_harmless(factory: sessionmaker[Session], seeded: Seeded) -> None:
    (run_id,) = make_runs(factory, seeded)
    with factory() as session:
        cancel_run(session, run_id)
        session.commit()
    with factory() as session:
        assert cancel_run(session, run_id).already_cancelled is True


def test_cancelling_a_finished_run_is_refused(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    (run_id,) = make_runs(factory, seeded)
    with factory() as session:
        run = session.get(EvaluationRun, run_id)
        assert run is not None
        run.status = EvaluationRunStatus.COMPLETED
        session.commit()
    with factory() as session, pytest.raises(OrchestrationError, match="已经跑完了"):
        cancel_run(session, run_id)


# ── 补跑 ────────────────────────────────────────────────────


def test_retry_failed_only_fills_holes(factory: sessionmaker[Session], seeded: Seeded) -> None:
    """三道题：一道有结论、一道还在排队、一道被取消留下窟窿 → 只补第三道。

    已经有认定结果的那道题一个字都不能碰：重跑它然后换掉结论，
    等于变相取多次里最好的一次（C-25 禁止）。
    """
    (run_id,) = make_runs(factory, seeded)
    decided, still_queued, hole = seeded.task_ids
    add_attempt(factory, run_id, decided, attempt_no=1, canonical=True)

    with factory() as session:
        # 第一道题的作业跑完了；第三道题的作业死在半路
        session.execute(
            sa.update(JobQueue)
            .where(JobQueue.payload["benchmark_task_id"].astext == str(decided))
            .values(state=JobState.DONE)
        )
        session.execute(
            sa.update(JobQueue)
            .where(JobQueue.payload["benchmark_task_id"].astext == str(hole))
            .values(state=JobState.DEAD, last_error="worker 崩了")
        )
        session.commit()

    with factory() as session:
        summary = retry_failed(session, run_id)
        session.commit()

    assert summary.requeued == (hole,)
    assert summary.already_decided == 1
    assert summary.still_running == 1
    new_jobs = [j for j in jobs_of(factory, run_id) if j.state is JobState.PENDING]
    assert {int(str(j.payload["benchmark_task_id"])) for j in new_jobs} == {still_queued, hole}
    refilled = next(j for j in new_jobs if int(str(j.payload["benchmark_task_id"])) == hole)
    assert refilled.priority == 1, "补跑要插在新题前面，否则实验永远定不了案"


def test_retry_failed_carries_the_previous_patch(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    """上一次已经拿到补丁了 → 补跑带上它的 key，由 StoredPatchRunner 重放（C-54）。

    不带的话下一次会重新调一遍被测 AI：AI 有随机性，那就不是"重跑"而是
    又采了一次样，还白花一次钱。
    """
    (run_id,) = make_runs(factory, seeded)
    task_id = seeded.task_ids[0]
    add_attempt(
        factory,
        run_id,
        task_id,
        attempt_no=1,
        canonical=False,
        infra=InfraOutcome.SANDBOX_ERROR,
        agent=None,
        lifecycle=LifecycleStatus.FAILED,
        with_patch=True,
    )
    with factory() as session:
        session.execute(
            sa.update(JobQueue)
            .where(JobQueue.payload["benchmark_task_id"].astext == str(task_id))
            .values(state=JobState.DEAD)
        )
        session.commit()

    with factory() as session:
        retry_failed(session, run_id)
        session.commit()

    job = next(
        j
        for j in jobs_of(factory, run_id)
        if j.state is JobState.PENDING and int(str(j.payload["benchmark_task_id"])) == task_id
    )
    assert job.payload["attempt_no"] == 2
    assert job.payload["reuse_patch_key"] == (f"runs/{run_id}/tasks/{task_id}/attempt-1/patch.diff")


def test_retry_failed_respects_the_global_attempt_cap(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    """一道题已经跑了 4 次 → 不再补（协议 C-71）。

    不设上限的话，不同故障类型会轮流重置各自的重试预算，一道坏题能把机时吃光。
    """
    (run_id,) = make_runs(factory, seeded)
    task_id = seeded.task_ids[0]
    for attempt in range(1, 5):
        add_attempt(
            factory,
            run_id,
            task_id,
            attempt_no=attempt,
            canonical=False,
            infra=InfraOutcome.SANDBOX_ERROR,
            agent=None,
            lifecycle=LifecycleStatus.FAILED,
        )
    with factory() as session:
        session.execute(sa.update(JobQueue).values(state=JobState.DEAD))
        session.commit()

    with factory() as session:
        summary = retry_failed(session, run_id)
        session.commit()

    assert task_id in summary.at_attempt_cap
    assert task_id not in summary.requeued


def test_retry_failed_is_refused_on_a_completed_run(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    """跑完的实验没有洞可补，要再跑一遍必须新建实验（协议 C-55）。"""
    (run_id,) = make_runs(factory, seeded)
    with factory() as session:
        run = session.get(EvaluationRun, run_id)
        assert run is not None
        run.status = EvaluationRunStatus.COMPLETED
        session.commit()
    with factory() as session, pytest.raises(OrchestrationError, match="C-55"):
        retry_failed(session, run_id)


def test_retry_failed_resumes_a_cancelled_run(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    """取消之后又决定接着跑：状态回到 RUNNING，否则补出来的结果永远不会被定案。"""
    (run_id,) = make_runs(factory, seeded)
    with factory() as session:
        cancel_run(session, run_id)
        session.commit()
    with factory() as session:
        summary = retry_failed(session, run_id)
        session.commit()
    assert len(summary.requeued) == 3
    with factory() as session:
        run = session.get(EvaluationRun, run_id)
        assert run is not None
        assert run.status is EvaluationRunStatus.RUNNING
        assert run.finished_at is None


# ── 进度与兜底定案 ──────────────────────────────────────────


def test_progress_is_written_back_to_the_run(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    (run_id,) = make_runs(factory, seeded)
    for task_id in seeded.task_ids:
        add_attempt(factory, run_id, task_id, attempt_no=1, canonical=True)
    with factory() as session:
        session.execute(sa.update(JobQueue).values(state=JobState.DONE))
        session.commit()

    with factory() as session:
        progress = refresh(session, run_id)
        session.commit()

    assert progress is not None
    assert progress.completed_tasks == 3
    assert progress.resolved_count == 3
    with factory() as session:
        run = session.get(EvaluationRun, run_id)
        assert run is not None
        assert run.status is EvaluationRunStatus.COMPLETED
        assert run.strict_resolve_rate is not None
        assert float(run.strict_resolve_rate) == 1.0
        assert run.finished_at is not None


def test_tasks_of_one_run_can_finish_at_the_same_time(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    """同一次实验的多条作业同时收尾，不能死锁，聚合出来的数也不能少。

    这是一条**回归测试**。2026-09-06 的 8 槽位实测里真撞过一次
    （作业 #133，`DeadlockDetected`，白等 60 秒退避才重试成功）：

    往 `evaluation_task_runs` 插一行时，Postgres 会顺手在父行
    （`evaluation_runs`）上加一把 `FOR KEY SHARE`。这把锁互相兼容，
    所以两条作业能同时拿到；等它们各自再去要 `FOR UPDATE` 更新进度时，
    就变成两边都在等对方放开 —— 锁升级死锁。

    修法是**先拿 `FOR UPDATE` 再插子表**（`progress.lock_run`）。
    顺序一旦被人改回去，这条测试会以死锁的形式失败。
    """
    (run_id,) = make_runs(factory, seeded)
    task_ids = seeded.task_ids
    ready = threading.Barrier(len(task_ids), timeout=30)
    errors: list[BaseException] = []

    def finish(task_id: int) -> None:
        try:
            ready.wait()  # 尽量让几个事务真的挤在一起
            with factory() as session:
                # 顺序就是被测的东西：先锁父行，再插子表
                lock_run(session, run_id)
                session.add(
                    EvaluationTaskRun(
                        evaluation_run_id=run_id,
                        benchmark_task_id=task_id,
                        attempt_no=1,
                        lifecycle_status=LifecycleStatus.COMPLETED,
                        infra_outcome=InfraOutcome.SUCCESS,
                        agent_outcome=AgentOutcome.RESOLVED,
                        is_canonical=True,
                        agent_started_at=datetime.now(tz=UTC),
                        prepare_started_at=datetime.now(tz=UTC),
                        completed_at=datetime.now(tz=UTC),
                    )
                )
                session.flush()
                refresh(session, run_id)
                session.commit()
        except BaseException as exc:  # 死锁在这里冒出来
            errors.append(exc)

    threads = [threading.Thread(target=finish, args=(task_id,)) for task_id in task_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"并发收尾出错了：{errors}"
    with factory() as session:
        run = session.get(EvaluationRun, run_id)
        assert run is not None
        assert run.completed_tasks == len(task_ids), "并发更新把进度算漏了"
        assert run.resolved_count == len(task_ids)


def test_stale_runs_are_finalized_by_the_sweep(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    """最后一条作业死了、没人收尾 → 兜底扫描把实验定案成 PARTIAL。

    没有这个兜底，实验会永远停在 RUNNING，前端的进度条也就永远停在那儿。
    降级成 PARTIAL 而不是 COMPLETED 是因为确实有一道题没有任何结论。
    """
    (run_id,) = make_runs(factory, seeded)
    add_attempt(factory, run_id, seeded.task_ids[0], attempt_no=1, canonical=True)
    with factory() as session:
        mark_running(session, run_id)
        session.execute(sa.update(JobQueue).values(state=JobState.DEAD))
        session.commit()

    with factory() as session:
        finalized = finalize_stale_runs(session)
        session.commit()

    assert finalized == [run_id]
    with factory() as session:
        run = session.get(EvaluationRun, run_id)
        assert run is not None
        assert run.status is EvaluationRunStatus.PARTIAL


def test_the_sweep_leaves_runs_with_live_jobs_alone(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    (run_id,) = make_runs(factory, seeded)
    with factory() as session:
        mark_running(session, run_id)
        session.commit()
    with factory() as session:
        assert finalize_stale_runs(session) == []
        session.commit()
    with factory() as session:
        run = session.get(EvaluationRun, run_id)
        assert run is not None
        assert run.status is EvaluationRunStatus.RUNNING
