"""EVAL_TASK 处理函数的落库与重试决定（E5-T1）。

**要真数据库，不要 Docker。** 评测结果用手工构造的 `TaskRunOutcome` 喂进去——
这一层要证明的是"跑完之后我们做了什么"，不是"跑得对不对"。真跑那条链在
`tests/sandbox/test_worker_eval_task.py`。

分开的理由和 `test_task_run_persistence.py` 一样：合成一条测试的话，
本地少起数据库或者少建镜像整片就被跳过，而跳过是不报错的。

验四件事：

1. 跑成功 → 落一条记录、打上 canonical、不再排下一次；
2. 可重试的故障 → 另投一条作业（`attempt_no` 加 1），**不**打 canonical；
3. 拿到过补丁的重试 → 作业里带上补丁的制品 key（协议 C-54，不许重新调 AI）；
4. canonical 不一定是编号最大的那条（协议 C-58），而且每题至多一条（C-57）。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import (
    AgentKind,
    ArtifactBackend,
    InfraOutcome,
    IssueLanguage,
    JobState,
    JobType,
    LifecycleStatus,
    PatchKind,
    TaskDifficulty,
)
from app.domain.enums import TestStatus as CaseStatus
from app.domain.protocol import INFRA_TO_AGENT_MAPPING, OutcomeRule
from app.evaluation.task_run import TaskRunOutcome, Timings
from app.infrastructure.config import Settings, get_settings
from app.infrastructure.db import create_session_factory
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.benchmark import (
    BenchmarkSet,
    BenchmarkTask,
    EnvironmentSpec,
    Repository,
)
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun
from app.infrastructure.models.job import JobQueue
from app.judge.decision import AgentFacts, judge
from app.judge.report_parser import ParsedReport, ReportSource
from app.judge.report_parser import TestCaseResult as CaseResult
from app.runner.patch import NormalizedPatch, patch_stats
from app.storage.base import ArtifactRef
from app.worker.handlers.eval_task import (
    EvalTaskPayload,
    _persist_and_schedule,
    enqueue_eval_task,
    run_key_for,
)
from app.worker.registry import JobContext

pytestmark = pytest.mark.db

START = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)

SAMPLE_PATCH = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-old\n"
    "+new\n"
    " ctx\n"
)


@pytest.fixture
def factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    """真提交的会话工厂。处理函数自己开事务，回滚型夹具在这里用不了。"""
    session_factory = create_session_factory(engine)
    try:
        yield session_factory
    finally:
        with session_factory() as session:
            session.execute(sa.delete(JobQueue))
            session.execute(sa.delete(EvaluationTaskRun))
            session.execute(sa.delete(EvaluationRun))
            session.execute(sa.delete(BenchmarkTask))
            session.execute(sa.delete(BenchmarkSet))
            session.execute(sa.delete(EnvironmentSpec))
            session.execute(sa.delete(AgentConfig))
            session.execute(sa.delete(Agent))
            session.execute(sa.delete(Repository))
            session.commit()


@pytest.fixture
def ids(factory: sessionmaker[Session]) -> tuple[int, int]:
    """建好一次实验和一道题，返回 `(evaluation_run_id, benchmark_task_id)`。"""
    with factory() as session:
        repo = Repository(full_name="bench-golden/textkit", url="golden://x", language="python")
        session.add(repo)
        session.flush()

        env = EnvironmentSpec(
            environment_id="bench-golden__textkit__py311",
            repository_id=repo.id,
            python_version="3.11",
            install_command="python -m pip install pytest",
            test_command="python -m pytest",
            test_report_path="report/junit.xml",
        )
        dataset = BenchmarkSet(slug="golden", version="v1", title="Golden Tasks")
        agent = Agent(
            name="mock", display_name="Mock", kind=AgentKind.MOCK, adapter_class="MockRunner"
        )
        session.add_all([env, dataset, agent])
        session.flush()

        config = AgentConfig(
            agent_id=agent.id,
            label="mock-default",
            agent_version="1.0",
            model_name="none",
            config_hash="0" * 64,
        )
        session.add(config)
        session.flush()

        task = BenchmarkTask(
            task_id="bench-golden__textkit-1",
            repository_id=repo.id,
            environment_spec_id=env.id,
            base_commit="a" * 40,
            issue_title="标题",
            issue_body="正文",
            issue_language=IssueLanguage.ZH,
            fail_to_pass=["tests/test_a.py::test_new"],
            pass_to_pass=[],
            test_patch_uri="local://test.patch",
            test_patch_paths=["tests/test_a.py"],
            gold_patch_uri="local://gold.patch",
            difficulty=TaskDifficulty.EASY,
            content_hash="1" * 64,
            raw_definition={},
        )
        run = EvaluationRun(name="e5t1", benchmark_set_id=dataset.id, agent_config_id=config.id)
        session.add_all([task, run])
        session.flush()
        session.commit()
        return run.id, task.id


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(
        update={"worker_id": "test-worker", "job_retry_backoff_base_s": 0.01, "job_max_attempts": 3}
    )


#: 这道题唯一那条 F2P 用例。跑成功的那条测试要有一份"它通过了"的报告。
F2P_ID = "tests/test_a.py::test_new"


def outcome_for(infra_outcome: InfraOutcome, *, with_patch: bool = False) -> TaskRunOutcome:
    """造一个终态结果。

    走 `judge()` 而不是自己拼三字段：那样才和真实路径用的是同一份规则，
    也顺带过一遍 C-78 的合法组合校验。

    `agent_started` **从 C-18 的映射表推**，不写死。映射成 `NOT_ATTEMPTED` 的故障
    （`ENV_BUILD_FAILED`、`WORKSPACE_ERROR`）按 C-69 必然发生在 AI 启动之前，
    传 True 会直接撞非法组合校验 —— 这正是这套校验存在的意义。
    """
    started = (
        INFRA_TO_AGENT_MAPPING[infra_outcome].outcome_rule is not OutcomeRule.FIXED_NOT_ATTEMPTED
    )
    report = (
        ParsedReport(
            cases={F2P_ID: CaseResult(F2P_ID, CaseStatus.PASSED, 10, None)},
            source=ReportSource.JUNIT_XML,
            aliases={},
            collection_errors=(),
            truncated=False,
            problem=None,
            skipped_without_id=0,
            xpass_may_read_as_passed=False,
        )
        if infra_outcome is InfraOutcome.SUCCESS
        else None
    )
    verdict = judge(
        infra_outcome=infra_outcome,
        report=report,
        fail_to_pass=(F2P_ID,) if infra_outcome is InfraOutcome.SUCCESS else (),
        facts=AgentFacts(agent_started=started),
        control_run_timed_out=False if infra_outcome is InfraOutcome.TEST_TIMEOUT else None,
    )
    patch = None
    patches: dict[PatchKind, ArtifactRef] = {}
    if with_patch:
        patch = NormalizedPatch(
            text=SAMPLE_PATCH,
            stats=patch_stats(SAMPLE_PATCH),
            raw_stats=patch_stats(SAMPLE_PATCH),
            filtered=(),
        )
        patches[PatchKind.AGENT_NORMALIZED] = ArtifactRef(
            key="runs/1/tasks/1/attempt-1/patch.diff",
            uri="local://runs/1/tasks/1/attempt-1/patch.diff.gz",
            backend=ArtifactBackend.LOCAL,
            content_type="text/x-diff",
            size_bytes=len(SAMPLE_PATCH),
            stored_bytes=len(SAMPLE_PATCH),
            sha256="2" * 64,
            compressed=True,
        )
    return TaskRunOutcome(
        verdict=verdict,
        timings=Timings(
            prepare_started_at=START,
            # C-69：agent_started_at 为空当且仅当 NOT_ATTEMPTED，两处必须一致
            agent_started_at=START if started else None,
            agent_finished_at=START if started else None,
            judged_at=START,
            completed_at=START,
        ),
        patch=patch,
        patches=patches,
        error_code=None if infra_outcome is InfraOutcome.SUCCESS else infra_outcome.value,
    )


def record(
    factory: sessionmaker[Session],
    settings: Settings,
    ids: tuple[int, int],
    infra_outcome: InfraOutcome,
    *,
    attempt_no: int = 1,
    with_patch: bool = False,
) -> None:
    """跑完一次 attempt 之后该做的那一整套（落库 + 决定重试 + 打标）。"""
    run_id, task_id = ids
    payload = EvalTaskPayload(
        evaluation_run_id=run_id, benchmark_task_id=task_id, attempt_no=attempt_no
    )
    with factory() as session:
        job = enqueue_eval_task(session, evaluation_run_id=run_id, benchmark_task_id=task_id)
        session.commit()
        job_id = job.id

    ctx = JobContext(
        job_id=job_id,
        job_type=JobType.EVAL_TASK,
        payload=payload.to_payload(),
        attempts=1,
        worker_id=settings.worker_id or "test-worker",
        settings=settings,
        session_factory=factory,
    )
    with factory() as session:
        _persist_and_schedule(
            session, ctx, payload, outcome_for(infra_outcome, with_patch=with_patch)
        )
        session.commit()


def attempts_of(factory: sessionmaker[Session]) -> list[EvaluationTaskRun]:
    with factory() as session:
        return list(
            session.execute(
                sa.select(EvaluationTaskRun).order_by(EvaluationTaskRun.attempt_no)
            ).scalars()
        )


def eval_jobs(factory: sessionmaker[Session]) -> list[JobQueue]:
    with factory() as session:
        return list(
            session.execute(
                sa.select(JobQueue)
                .where(JobQueue.job_type == JobType.EVAL_TASK)
                .order_by(JobQueue.id)
            ).scalars()
        )


# ── 跑成功 ──────────────────────────────────────────────────


def test_a_successful_run_is_canonical_and_final(
    factory: sessionmaker[Session], settings: Settings, ids: tuple[int, int]
) -> None:
    record(factory, settings, ids, InfraOutcome.SUCCESS)

    rows = attempts_of(factory)
    assert len(rows) == 1
    assert rows[0].attempt_no == 1
    assert rows[0].infra_outcome is InfraOutcome.SUCCESS
    assert rows[0].lifecycle_status is LifecycleStatus.COMPLETED
    assert rows[0].is_canonical is True
    assert rows[0].worker_id == "test-worker"
    # 没有第二条作业被投出来
    assert len(eval_jobs(factory)) == 1


def test_queued_at_comes_from_the_job_not_from_now(
    factory: sessionmaker[Session], settings: Settings, ids: tuple[int, int]
) -> None:
    """`queued_at` 记的是进队列的时刻，不是落库的时刻。

    用当前时间的话这个字段永远约等于 `completed_at`，等于没记 ——
    而它要回答的是"这道题排了多久队"，是容量模型的输入。

    所以直接跟作业行的 `created_at` 对齐。**不要**改成跟 `completed_at` 比大小：
    这个测试里的 `completed_at` 是写死的假时间戳（`START`），而 `queued_at`
    走的是数据库真实时钟，两者比大小只是在比"现在几点"，CI 上必然翻车。
    """
    record(factory, settings, ids, InfraOutcome.SUCCESS)
    row = attempts_of(factory)[0]
    assert row.queued_at is not None
    assert row.queued_at == eval_jobs(factory)[0].created_at


# ── 可重试的故障 ────────────────────────────────────────────


def test_a_retryable_failure_schedules_the_next_attempt(
    factory: sessionmaker[Session], settings: Settings, ids: tuple[int, int]
) -> None:
    """`ENV_BUILD_FAILED` 可重试 → 另投一条作业，而且**不**打 canonical。

    这一条同时验了两件事：重试走的是"新作业 + 新 attempt_no"（协议 C-32），
    以及重试没结束之前不能定案（C-24）。
    """
    record(factory, settings, ids, InfraOutcome.ENV_BUILD_FAILED)

    rows = attempts_of(factory)
    assert len(rows) == 1
    assert rows[0].is_canonical is False, "还要重试，现在定不了案"

    jobs = eval_jobs(factory)
    assert len(jobs) == 2, "应该投出下一次 attempt 的作业"
    assert jobs[1].payload["attempt_no"] == 2
    assert jobs[1].payload["retry_of_id"] == rows[0].id
    assert jobs[1].priority == 1, "重试要插在新题前面，否则一道题会拖着整次实验"
    assert jobs[1].state is JobState.PENDING


def test_a_post_patch_failure_carries_the_patch_forward(
    factory: sessionmaker[Session], settings: Settings, ids: tuple[int, int]
) -> None:
    """协议 C-54：已经拿到补丁了，重试必须复用它，不许重新调 AI。

    落点就是重试作业里的 `reuse_patch_key`。没有它，下一次会重新跑一遍被测 AI——
    AI 有随机性，那就不是"重跑"而是又采了一次样（C-25 禁止取最优），还白花一次钱。
    """
    record(factory, settings, ids, InfraOutcome.TEST_TIMEOUT, with_patch=True)

    jobs = eval_jobs(factory)
    assert len(jobs) == 2
    assert jobs[1].payload["reuse_patch_key"] == "runs/1/tasks/1/attempt-1/patch.diff"


def test_a_failure_without_a_patch_does_not_reuse_anything(
    factory: sessionmaker[Session], settings: Settings, ids: tuple[int, int]
) -> None:
    """补丁还没拿到就挂了（比如工作区物化失败），重试当然要重新跑 AI。

    这里 `reuse_patch_key` 必须是 None —— 带上一个不存在的 key，
    下一次会在读制品那一步直接失败。
    """
    record(factory, settings, ids, InfraOutcome.WORKSPACE_ERROR)
    assert eval_jobs(factory)[1].payload["reuse_patch_key"] is None


def test_the_last_attempt_becomes_canonical_when_the_budget_runs_out(
    factory: sessionmaker[Session], settings: Settings, ids: tuple[int, int]
) -> None:
    """同一种故障连来两次，`ENV_BUILD_FAILED` 的预算用完 → 第 2 条定案。"""
    record(factory, settings, ids, InfraOutcome.ENV_BUILD_FAILED, attempt_no=1)
    record(factory, settings, ids, InfraOutcome.ENV_BUILD_FAILED, attempt_no=2)

    rows = attempts_of(factory)
    assert [r.attempt_no for r in rows] == [1, 2]
    assert [r.is_canonical for r in rows] == [False, True]


# ── C-58 / C-57 ─────────────────────────────────────────────


def test_canonical_is_the_first_non_retryable_not_the_last_attempt(
    factory: sessionmaker[Session], settings: Settings, ids: tuple[int, int]
) -> None:
    """协议 C-58：**禁止**靠"取最大的 attempt_no"推断 canonical。

    第 1 次就 `AGENT_TIMEOUT`（不可重试），它就是认定结果。这里再手工造一条
    编号更大的 attempt（模拟 E5-T2 的人工重跑），canonical 不能被抢走。
    """
    record(factory, settings, ids, InfraOutcome.AGENT_TIMEOUT, attempt_no=1)
    assert attempts_of(factory)[0].is_canonical is True

    record(factory, settings, ids, InfraOutcome.SANDBOX_ERROR, attempt_no=2)

    rows = attempts_of(factory)
    assert [r.attempt_no for r in rows] == [1, 2]
    assert rows[0].is_canonical is True, "第 1 次那条才是认定结果"
    assert rows[1].is_canonical is False


def test_at_most_one_canonical_per_task(
    factory: sessionmaker[Session], settings: Settings, ids: tuple[int, int]
) -> None:
    """协议 C-57 的部分唯一索引真的拦得住第二条 canonical。

    这是最后一道闸。解决率按 canonical 取数，一题两条 canonical 会让它被数两遍，
    而且哪条算数完全取决于查询的排序，同一份数据能得出不同的排行榜。
    """
    run_id, task_id = ids
    record(factory, settings, ids, InfraOutcome.SUCCESS, attempt_no=1)

    with factory() as session, pytest.raises(IntegrityError):
        session.add(
            EvaluationTaskRun(
                evaluation_run_id=run_id,
                benchmark_task_id=task_id,
                attempt_no=2,
                lifecycle_status=LifecycleStatus.QUEUED,
                is_canonical=True,
            )
        )
        session.flush()


# ── payload ────────────────────────────────────────────────


def test_payload_round_trips() -> None:
    """payload 进 JSONB 再出来必须一模一样，字段名拼错了要在这里就发现。"""
    payload = EvalTaskPayload(
        evaluation_run_id=7,
        benchmark_task_id=9,
        attempt_no=3,
        retry_of_id=42,
        reuse_patch_key="runs/7/tasks/9/attempt-2/patch.diff",
    )
    assert EvalTaskPayload.from_payload(payload.to_payload()) == payload


def test_run_key_is_derivable_from_the_ids() -> None:
    """制品 key 由 id 直接推得出来，不用先查 `artifacts` 表。"""
    payload = EvalTaskPayload(evaluation_run_id=7, benchmark_task_id=9, attempt_no=3)
    assert run_key_for(payload) == "runs/7/tasks/9/attempt-3"
