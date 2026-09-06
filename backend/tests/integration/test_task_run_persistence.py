"""落库的集成测试（E4-T4，M1 里程碑的另一半）。

**要一个真的 PostgreSQL，但不要 Docker 跑评测** —— 结果用手工构造的
`TaskRunOutcome` 喂进去。全链路那半在 `tests/sandbox/test_task_run.py`（要 Docker）。

分开的理由是：合成一条测试的话，本地少起数据库或者少建镜像，整片就被跳过 ——
而跳过是不报错的，看起来像全过了。

这一层要证明三件事：

1. 结论、时刻、统计、逐条用例、两份补丁、日志制品**都真的写进去了**；
2. 数据库那条 `legal_combination` CHECK 约束**真的拦得住**非法组合（协议 C-78）；
3. 分母算的是"题目列出的条数"，`MISSING` 也占分母 —— 否则用例找不到会让分母缩水，
   通过率反而变好看。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.enums import (
    AgentKind,
    AgentOutcome,
    ArtifactBackend,
    ArtifactKind,
    CostSource,
    InfraOutcome,
    IssueLanguage,
    LifecycleStatus,
    PatchKind,
    TaskDifficulty,
)
from app.domain.enums import TestRole as Role
from app.domain.enums import TestStatus as Status
from app.evaluation.persistence import persist_task_run
from app.evaluation.task_run import TaskRunOutcome, Timings
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.artifact import Artifact
from app.infrastructure.models.benchmark import (
    BenchmarkSet,
    BenchmarkTask,
    EnvironmentSpec,
    Repository,
)
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun, PatchArtifact

# 别名不能还是 Test 开头 —— pytest 按 `Test*` 收集类，`TestResultRow` 一样会被抓。
from app.infrastructure.models.evaluation import TestResult as CaseRow
from app.judge.decision import CaseVerdict, Verdict
from app.runner.patch import FilteredChange, FilterReason, NormalizedPatch, patch_stats
from app.runner.protocol import AgentRunResult, TokenUsage
from app.storage.base import ArtifactRef

pytestmark = pytest.mark.db

START = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)

#: 一段能算出真实统计的小补丁。手写常量统计的话，哪天 `patch_stats` 改了行为，
#: 这里的断言还是绿的，而真实落库的数字已经错了。
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
def task_run(session: Session) -> EvaluationTaskRun:
    """建好一条 QUEUED 的执行记录，外加它上游那一串外键。

    真实评测里这些行由 E1-T3（题目入库）和 E5（编排层领作业）建，
    这里手工造出最小的一套 —— 落库这件事要验的是"写进去的内容对不对"，
    不该被上游还没做完挡住。
    """
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
    agent = Agent(name="mock", display_name="Mock", kind=AgentKind.MOCK, adapter_class="MockRunner")
    session.add_all([env, dataset, agent])
    session.flush()

    config = AgentConfig(
        agent_id=agent.id,
        label="mock-default",
        agent_version="1.0",
        model_name="none",
        config_hash="0" * 64,  # CHAR(64)：只存十六进制，不带 `sha256:` 前缀
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
        pass_to_pass=["tests/test_a.py::test_old"],
        test_patch_uri="local://test.patch",
        test_patch_paths=["tests/test_a.py"],
        gold_patch_uri="local://gold.patch",
        difficulty=TaskDifficulty.EASY,
        content_hash="1" * 64,
        raw_definition={},
    )
    run = EvaluationRun(name="m1-smoke", benchmark_set_id=dataset.id, agent_config_id=config.id)
    session.add_all([task, run])
    session.flush()

    row = EvaluationTaskRun(
        evaluation_run_id=run.id,
        benchmark_task_id=task.id,
        attempt_no=1,
        lifecycle_status=LifecycleStatus.QUEUED,
    )
    session.add(row)
    session.flush()
    return row


def make_patch(text: str = SAMPLE_PATCH, *, protected: bool = False) -> NormalizedPatch:
    """造一个 `NormalizedPatch`，统计由 `patch_stats` 真算，不手写常量。"""
    filtered = (
        (FilteredChange("tests/test_a.py", FilterReason.PROTECTED_PATH, "命中受保护路径"),)
        if protected
        else ()
    )
    return NormalizedPatch(
        text=text, stats=patch_stats(text), raw_stats=patch_stats(text), filtered=filtered
    )


def make_outcome(
    *,
    agent_outcome: AgentOutcome | None = AgentOutcome.RESOLVED,
    infra_outcome: InfraOutcome = InfraOutcome.SUCCESS,
    lifecycle: LifecycleStatus = LifecycleStatus.COMPLETED,
    cases: tuple[CaseVerdict, ...] = (),
    patch: NormalizedPatch | None = None,
    agent_started: bool = True,
    agent_result: AgentRunResult | None = None,
) -> TaskRunOutcome:
    if not cases:
        cases = (
            CaseVerdict("tests/test_a.py::test_new", Role.F2P, Status.PASSED, 12, None),
            CaseVerdict("tests/test_a.py::test_old", Role.P2P, Status.PASSED, 8, None),
        )
    return TaskRunOutcome(
        verdict=Verdict(
            lifecycle_status=lifecycle,
            infra_outcome=infra_outcome,
            agent_outcome=agent_outcome,
            counts_as_infra_failure=False,
            cases=cases,
            f2p_ok=all(c.passed for c in cases if c.role is Role.F2P),
            p2p_ok=all(c.passed for c in cases if c.role is Role.P2P),
        ),
        timings=Timings(
            prepare_started_at=START,
            agent_started_at=START + timedelta(seconds=1) if agent_started else None,
            agent_finished_at=START + timedelta(seconds=3) if agent_started else None,
            test_started_at=START + timedelta(seconds=4),
            test_finished_at=START + timedelta(seconds=9),
            judged_at=START + timedelta(seconds=9),
            completed_at=START + timedelta(seconds=10),
        ),
        patch=patch if patch is not None else make_patch(),
        patches={
            PatchKind.AGENT_RAW: _ref("runs/1/patch-raw.diff"),
            PatchKind.AGENT_NORMALIZED: _ref("runs/1/patch.diff"),
        },
        artifacts={ArtifactKind.TEST_STDOUT: _ref("runs/1/test.log")},
        agent_result=agent_result,
    )


def make_agent_result(usage: TokenUsage | None) -> AgentRunResult:
    """一份最简的适配器结果，只为把 token / cost 那几列喂进去。"""
    return AgentRunResult(
        agent_name="aider",
        agent_version="0.86.2",
        model="deepseek/deepseek-chat",
        started_at=START,
        finished_at=START + timedelta(seconds=3),
        duration_ms=3000,
        patch=SAMPLE_PATCH,
        token_usage=usage,
        cost_usd=0.0042,
        cost_source=CostSource.REPORTED,
        turns=2,
    )


def _ref(key: str) -> ArtifactRef:
    return ArtifactRef(
        key=key,
        uri=f"local://{key}.gz",
        backend=ArtifactBackend.LOCAL,
        content_type="text/plain; charset=utf-8",
        size_bytes=128,
        stored_bytes=40,
        sha256="2" * 64,
        compressed=True,
    )


# ── 写进去的内容对不对 ──────────────────────────────────────


def test_verdict_and_timings_are_written(session: Session, task_run: EvaluationTaskRun) -> None:
    persist_task_run(session, task_run, make_outcome())

    row = session.get(EvaluationTaskRun, task_run.id)
    assert row is not None
    assert row.lifecycle_status is LifecycleStatus.COMPLETED
    assert row.infra_outcome is InfraOutcome.SUCCESS
    assert row.agent_outcome is AgentOutcome.RESOLVED
    assert row.agent_started_at is not None
    assert row.agent_duration_ms == 2000
    assert row.test_duration_ms == 5000
    assert row.total_duration_ms == 10000


def test_token_usage_lands_in_the_columns(session: Session, task_run: EvaluationTaskRun) -> None:
    """token 和 cost 四项都要落库，缓存命中单独一列。"""
    usage = TokenUsage(input=6800, output=625, cache_read=4800, total=7425)
    persist_task_run(session, task_run, make_outcome(agent_result=make_agent_result(usage)))
    session.flush()

    assert task_run.tokens_input == 6800
    assert task_run.tokens_output == 625
    assert task_run.tokens_cache_read == 4800
    assert task_run.tokens_total == 7425
    assert task_run.turns == 2
    assert task_run.cost_source is CostSource.REPORTED


def test_cache_hits_are_not_added_into_the_total(
    session: Session, task_run: EvaluationTaskRun
) -> None:
    """缓存命中是 `input` 的一部分，不是另加的一份。

    加进 `tokens_total` 的话，token 统计会凭空多出一截 —— 实测 DeepSeek 每轮
    命中 2.4k 左右，四道题一轮就能把总量吹高一倍多，而且没人看得出来。
    """
    usage = TokenUsage(input=6800, output=625, cache_read=4800, total=7425)
    persist_task_run(session, task_run, make_outcome(agent_result=make_agent_result(usage)))
    session.flush()

    assert task_run.tokens_total == task_run.tokens_input + task_run.tokens_output
    assert task_run.tokens_cache_read is not None
    assert task_run.tokens_cache_read < task_run.tokens_input


def test_no_usage_leaves_the_columns_null_not_zero(
    session: Session, task_run: EvaluationTaskRun
) -> None:
    """报不出用量时留空，不能填 0。

    空是"这个适配器报不出来"，0 是"报得出来、确实一次都没有"。填 0 的话，
    一次连响应都没拿到的运行（比如 AI 卡在复读循环里被墙钟杀掉）会被追认成
    "确实没命中过缓存"，成本分析拿它当真值就错了。和 `cost_usd` 同一条纪律。
    """
    persist_task_run(session, task_run, make_outcome(agent_result=make_agent_result(None)))
    session.flush()

    assert task_run.tokens_input is None
    assert task_run.tokens_cache_read is None


def test_patch_stats_are_written(session: Session, task_run: EvaluationTaskRun) -> None:
    """补丁统计要落到宽表上 —— 报表和归因直接读这几列，不去解析补丁正文。"""
    outcome = make_outcome()
    persist_task_run(session, task_run, outcome)

    row = session.get(EvaluationTaskRun, task_run.id)
    assert row is not None
    assert outcome.patch is not None
    assert row.files_changed == outcome.patch.stats.files_changed
    assert row.lines_added == outcome.patch.stats.lines_added
    assert row.lines_deleted == outcome.patch.stats.lines_deleted


def test_c08b_diagnostic_fields_are_written(session: Session, task_run: EvaluationTaskRun) -> None:
    """协议 C-08b 的三个诊断字段。

    少了它们，"AI 什么都没做"和"AI 改的全是受保护文件、被我们丢光了"
    在数据上完全一样 —— 而这是两种截然不同的行为。
    """
    persist_task_run(session, task_run, make_outcome(patch=make_patch(protected=True)))

    row = session.get(EvaluationTaskRun, task_run.id)
    assert row is not None
    assert row.raw_patch_empty is False
    assert row.protected_path_edit_attempted is True
    assert row.filtered_change_reasons
    assert row.filtered_change_reasons[0]["reason"] == FilterReason.PROTECTED_PATH.value


def test_per_case_results_are_written(session: Session, task_run: EvaluationTaskRun) -> None:
    """逐条用例入库 —— 这是"结论可查"的基础。"""
    persist_task_run(session, task_run, make_outcome())

    rows = session.scalars(
        sa.select(CaseRow).where(CaseRow.evaluation_task_run_id == task_run.id)
    ).all()
    by_id = {row.test_id: row for row in rows}
    assert by_id["tests/test_a.py::test_new"].role is Role.F2P
    assert by_id["tests/test_a.py::test_old"].role is Role.P2P
    assert all(row.status is Status.PASSED for row in rows)


def test_missing_cases_are_written_too(session: Session, task_run: EvaluationTaskRun) -> None:
    """`MISSING` 的用例也要写。

    它们正是复核任务要看的东西（C-13b 第 2 项要求把题目里的 ID 和报告里的 ID
    摆在一起对照）。不写的话，查起来只剩一个数字对不上。
    """
    cases = (
        CaseVerdict("tests/test_a.py::test_new", Role.F2P, Status.MISSING),
        CaseVerdict("tests/test_a.py::test_old", Role.P2P, Status.PASSED, 8, None),
    )
    persist_task_run(
        session, task_run, make_outcome(agent_outcome=AgentOutcome.UNRESOLVED, cases=cases)
    )

    row = session.get(EvaluationTaskRun, task_run.id)
    assert row is not None
    stored = session.scalars(sa.select(CaseRow).where(CaseRow.status == Status.MISSING)).all()
    assert len(stored) == 1
    # 分母是**题目列出的条数**，MISSING 也占分母 —— 否则"用例找不到"会让分母缩水，
    # 通过率反而变好看
    assert (row.f2p_passed, row.f2p_total) == (0, 1)
    assert (row.p2p_passed, row.p2p_total) == (1, 1)


def test_both_patches_are_written(session: Session, task_run: EvaluationTaskRun) -> None:
    """原始补丁和标准化补丁两份都要有行。

    只写标准化的话，"AI 试图改测试文件"这个行为就再也查不到了（C-08b）。
    """
    persist_task_run(session, task_run, make_outcome())

    rows = session.scalars(
        sa.select(PatchArtifact).where(PatchArtifact.evaluation_task_run_id == task_run.id)
    ).all()
    assert {row.kind for row in rows} == {PatchKind.AGENT_RAW, PatchKind.AGENT_NORMALIZED}

    row = session.get(EvaluationTaskRun, task_run.id)
    assert row is not None
    normalized = next(r for r in rows if r.kind is PatchKind.AGENT_NORMALIZED)
    assert row.patch_artifact_id == normalized.id, "宽表要指向标准化的那一份"


def test_artifacts_are_indexed(session: Session, task_run: EvaluationTaskRun) -> None:
    """日志类制品只在库里留索引行，内容在制品存储里（可达数 MB，不入库）。"""
    persist_task_run(session, task_run, make_outcome())

    rows = session.scalars(
        sa.select(Artifact).where(
            Artifact.owner_id == task_run.id, Artifact.kind == ArtifactKind.TEST_STDOUT
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].uri.startswith("local://"), "存的是物理位置，不是逻辑 key"
    assert rows[0].size_bytes == 128


# ── 数据库那道防线 ──────────────────────────────────────────


def test_database_rejects_an_illegal_combination(
    session: Session, task_run: EvaluationTaskRun
) -> None:
    """绕过判定引擎直接写非法组合 → 数据库的 CHECK 约束必须拦住（协议 C-78）。

    这道防线是给"以后可能出现的手工修数据、数据迁移、别的服务写入"准备的。
    真让一条非法组合落了库，排行榜会算出一个谁也解释不了的数字，
    而且事后无法区分是判定错了还是写库错了。
    """
    task_run.lifecycle_status = LifecycleStatus.COMPLETED
    task_run.infra_outcome = InfraOutcome.WORKSPACE_ERROR
    # WORKSPACE_ERROR 只能配 FAILED + NOT_ATTEMPTED，配 RESOLVED 是非法的
    task_run.agent_outcome = AgentOutcome.RESOLVED

    with pytest.raises(IntegrityError):
        session.flush()


def test_persist_refuses_an_illegal_combination_before_touching_the_row(
    session: Session, task_run: EvaluationTaskRun
) -> None:
    """`persist_task_run` 自己也要在写之前拦一次，报的错要能读懂。

    等数据库报 `IntegrityError` 也拦得住，但那条报错只说"违反了 legal_combination
    约束"，看的人得自己去翻协议 §4.3。这里抛的异常直接把三个字段的值和出处印出来。
    """
    from app.domain.protocol import IllegalCombinationError

    outcome = make_outcome(
        lifecycle=LifecycleStatus.COMPLETED,
        infra_outcome=InfraOutcome.WORKSPACE_ERROR,
        agent_outcome=AgentOutcome.RESOLVED,
    )
    with pytest.raises(IllegalCombinationError, match=r"§4\.3"):
        persist_task_run(session, task_run, outcome)

    assert task_run.lifecycle_status is LifecycleStatus.QUEUED, "拦下来时不该已经改过行"


def test_not_attempted_requires_no_agent_start(
    session: Session, task_run: EvaluationTaskRun
) -> None:
    """`NOT_ATTEMPTED` 落库时 `agent_started_at` 必须为空（C-69、C-77）。"""
    persist_task_run(
        session,
        task_run,
        make_outcome(
            lifecycle=LifecycleStatus.FAILED,
            infra_outcome=InfraOutcome.WORKSPACE_ERROR,
            agent_outcome=AgentOutcome.NOT_ATTEMPTED,
            agent_started=False,
        ),
    )

    row = session.get(EvaluationTaskRun, task_run.id)
    assert row is not None
    assert row.agent_outcome is AgentOutcome.NOT_ATTEMPTED
    assert row.agent_started_at is None
