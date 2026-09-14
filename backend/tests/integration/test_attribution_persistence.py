"""归因落库的集成测试（E6-T1）。

盯三条 AC：

- 重跑不产生重复行（`UNIQUE(evaluation_task_run_id)` 上的 upsert）
- **不覆盖 `stage = LLM / HUMAN` 的结论** —— 规则重跑一遍就把人工抽检改过的
  结论盖掉，抽检等于白做
- 基线报错取自同一份数据集快照的 **Noop 哨兵**运行（`features` 的模块文档说了为什么）
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.attribution.persistence import (
    collect_features,
    existing_stages,
    load_facts,
    save_rule_verdicts,
)
from app.attribution.rules import RuleName, RuleVerdict, classify
from app.domain.enums import (
    AgentKind,
    AgentOutcome,
    AttributionStage,
    AttributionStatus,
    BenchmarkSetStatus,
    EvaluationRunStatus,
    FailureCategory,
    InfraOutcome,
    IssueLanguage,
    LifecycleStatus,
    PatchKind,
    TaskDifficulty,
    TaskValidationState,
    TestRole,
    TestStatus,
)
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.attribution import FailureAttribution
from app.infrastructure.models.benchmark import (
    BenchmarkSet,
    BenchmarkTask,
    EnvironmentSpec,
    Repository,
)
from app.infrastructure.models.evaluation import (
    EvaluationRun,
    EvaluationTaskRun,
    PatchArtifact,
    TestResult,
)
from app.storage.base import ArtifactStore
from app.storage.local import LocalArtifactStore
from tests.integration.conftest import count_queries
from tests.integration.factories import wipe

pytestmark = pytest.mark.db


class World:
    """一个数据集快照 + 一道题 + 想建几个参赛者建几个。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        repo = Repository(full_name="pallets/click", url="https://x", language="python")
        session.add(repo)
        session.flush()
        env = EnvironmentSpec(
            environment_id="pallets__click__py311",
            repository_id=repo.id,
            python_version="3.11",
            install_command="pip install -e .",
            test_command="pytest",
            test_report_path="report/junit.xml",
        )
        self.dataset = BenchmarkSet(
            slug="benchmark-dev",
            version="v1",
            title="开发集",
            status=BenchmarkSetStatus.PUBLISHED,
            task_count=1,
        )
        session.add_all([env, self.dataset])
        session.flush()
        task = BenchmarkTask(
            task_id="pallets__click-1",
            repository_id=repo.id,
            environment_spec_id=env.id,
            base_commit="a" * 40,
            issue_title="标题",
            issue_body="正文",
            issue_language=IssueLanguage.EN,
            fail_to_pass=["tests/test_a.py::test_new"],
            pass_to_pass=[],
            test_patch_uri="local://test.patch",
            test_patch_paths=["tests/test_a.py"],
            gold_patch_uri="local://gold.patch",
            difficulty=TaskDifficulty.EASY,
            validation_state=TaskValidationState.VALID,
            content_hash="0" * 64,
            raw_definition={},
        )
        session.add(task)
        session.flush()
        self.task_id = int(task.id)

    def task_run(
        self,
        *,
        kind: AgentKind = AgentKind.CLI,
        name: str = "aider",
        agent_outcome: AgentOutcome | None = AgentOutcome.UNRESOLVED,
        infra_outcome: InfraOutcome = InfraOutcome.SUCCESS,
        f2p: tuple[int, int] = (0, 1),
        p2p: tuple[int, int] = (10, 10),
    ) -> int:
        agent = Agent(name=name, display_name=name, kind=kind, adapter_class=f"{name}Runner")
        self.session.add(agent)
        self.session.flush()
        config = AgentConfig(
            agent_id=agent.id,
            label=f"{name}@x",
            agent_version="1.0",
            model_name="m",
            config_hash=f"{agent.id:064d}",
        )
        self.session.add(config)
        self.session.flush()
        run = EvaluationRun(
            name="r",
            benchmark_set_id=self.dataset.id,
            agent_config_id=config.id,
            status=EvaluationRunStatus.COMPLETED,
            total_tasks=1,
            completed_tasks=1,
            resolved_count=0,
            infra_failure_count=0,
            protocol_version="v1.2",
            manifest={},
        )
        self.session.add(run)
        self.session.flush()
        task_run = EvaluationTaskRun(
            evaluation_run_id=run.id,
            benchmark_task_id=self.task_id,
            lifecycle_status=LifecycleStatus.COMPLETED,
            # 必填：库里有条协议约束（C-69），agent_started_at 为空时
            # agent_outcome 只能是 NOT_ATTEMPTED，不填就写不进去
            agent_started_at=run.created_at,
            infra_outcome=infra_outcome,
            agent_outcome=agent_outcome,
            f2p_passed=f2p[0],
            f2p_total=f2p[1],
            p2p_passed=p2p[0],
            p2p_total=p2p[1],
            raw_patch_empty=False,
            protected_path_edit_attempted=False,
            is_canonical=True,
        )
        self.session.add(task_run)
        self.session.flush()
        return int(task_run.id)

    def with_patches(
        self, task_run_id: int, store: ArtifactStore, *, agent_diff: str, gold_diff: str
    ) -> None:
        """给一次运行配上 AI 的补丁（进制品库）和官方补丁（进 raw_definition）。

        官方补丁**故意不落制品库** —— 库里真实的样子就是这样，见 `_gold_patch` 的文档。
        """
        key = f"runs/x/tasks/{task_run_id}/patch.diff"
        store.put(key, agent_diff.encode("utf-8"), content_type="text/x-diff")
        self.session.add(
            PatchArtifact(
                evaluation_task_run_id=task_run_id,
                kind=PatchKind.AGENT_NORMALIZED,
                uri=f"local://{key}",
                sha256="0" * 64,
                size_bytes=len(agent_diff),
                files_changed=1,
                lines_added=1,
                lines_deleted=0,
                is_empty=False,
            )
        )
        task = self.session.get(BenchmarkTask, self.task_id)
        assert task is not None
        task.raw_definition = {"gold_patch": gold_diff}
        self.session.flush()

    def f2p_message(self, task_run_id: int, test_id: str, message: str | None) -> None:
        self.session.add(
            TestResult(
                evaluation_task_run_id=task_run_id,
                test_id=test_id,
                role=TestRole.F2P,
                status=TestStatus.FAILED if message else TestStatus.PASSED,
                message_excerpt=message,
            )
        )
        self.session.flush()


@pytest.fixture(autouse=True)
def clean(session: Session) -> None:
    wipe(session)


@pytest.fixture
def world(session: Session) -> World:
    return World(session)


@pytest.fixture
def artifact_store(tmp_path: Path) -> ArtifactStore:
    """空的制品库。这两条测试只看报错比对，补丁和日志本来就该标 unavailable。"""
    return LocalArtifactStore(tmp_path / "artifacts")


def rule_items(session: Session) -> list[tuple[int, RuleVerdict]]:
    """把全库跑一遍规则，只留判死的。"""
    out: list[tuple[int, RuleVerdict]] = []
    for row in load_facts(session):
        result = classify(row.facts)
        if isinstance(result, RuleVerdict):
            out.append((row.task_run_id, result))
    return out


def test_rule_verdict_is_saved(world: World, session: Session) -> None:
    task_run_id = world.task_run(agent_outcome=AgentOutcome.EMPTY_PATCH)
    session.commit()

    report = save_rule_verdicts(session, rule_items(session))
    session.commit()

    assert report.inserted == 1
    assert report.updated == 0
    row = session.scalar(
        select(FailureAttribution).where(FailureAttribution.evaluation_task_run_id == task_run_id)
    )
    assert row is not None
    assert row.category is FailureCategory.F7_EMPTY_OR_INVALID_PATCH
    assert row.stage is AttributionStage.RULE
    assert row.status is AttributionStatus.OK
    assert row.evidence["rule"] == RuleName.EMPTY_OR_INVALID_PATCH.value


def test_rerun_does_not_duplicate_rows(world: World, session: Session) -> None:
    world.task_run(agent_outcome=AgentOutcome.EMPTY_PATCH)
    session.commit()

    save_rule_verdicts(session, rule_items(session))
    session.commit()
    second = save_rule_verdicts(session, rule_items(session))
    session.commit()

    assert second.inserted == 0
    assert second.updated == 1
    rows = session.scalars(select(FailureAttribution)).all()
    assert len(rows) == 1


def test_human_verdict_is_never_overwritten(world: World, session: Session) -> None:
    """人工抽检改过的结论，规则重跑一遍不许动。"""
    task_run_id = world.task_run(agent_outcome=AgentOutcome.EMPTY_PATCH)
    session.add(
        FailureAttribution(
            evaluation_task_run_id=task_run_id,
            stage=AttributionStage.HUMAN,
            category=FailureCategory.N2_TASK_DEFECT,
            evidence={"note": "抽检认定题目本身有问题"},
            status=AttributionStatus.OK,
        )
    )
    session.commit()

    report = save_rule_verdicts(session, rule_items(session))
    session.commit()

    assert report.protected == 1
    assert report.inserted == 0
    row = session.scalar(
        select(FailureAttribution).where(FailureAttribution.evaluation_task_run_id == task_run_id)
    )
    assert row is not None
    assert row.stage is AttributionStage.HUMAN, "人工结论被盖掉了"
    assert row.category is FailureCategory.N2_TASK_DEFECT


def test_llm_verdict_is_never_overwritten(world: World, session: Session) -> None:
    task_run_id = world.task_run(agent_outcome=AgentOutcome.EMPTY_PATCH)
    session.add(
        FailureAttribution(
            evaluation_task_run_id=task_run_id,
            stage=AttributionStage.LLM,
            category=FailureCategory.F3_INCOMPLETE_FIX,
            evidence={},
            status=AttributionStatus.OK,
        )
    )
    session.commit()

    report = save_rule_verdicts(session, rule_items(session))
    session.commit()

    assert report.protected == 1
    stages = existing_stages(session)
    assert stages[task_run_id] is AttributionStage.LLM


def test_load_facts_carries_enough_for_the_regression_rule(world: World, session: Session) -> None:
    task_run_id = world.task_run(f2p=(1, 1), p2p=(9, 10))
    session.commit()

    rows = {row.task_run_id: row.facts for row in load_facts(session)}
    verdict = classify(rows[task_run_id])
    assert isinstance(verdict, RuleVerdict)
    assert verdict.category is FailureCategory.F6_REGRESSION


def test_baseline_messages_come_from_the_noop_sentinel(
    world: World, session: Session, artifact_store: ArtifactStore
) -> None:
    noop_run = world.task_run(
        kind=AgentKind.NOOP, name="noop", agent_outcome=AgentOutcome.EMPTY_PATCH
    )
    world.f2p_message(noop_run, "tests/test_a.py::test_new", "AttributeError: 没有 add")
    agent_run = world.task_run(kind=AgentKind.CLI, name="aider")
    world.f2p_message(agent_run, "tests/test_a.py::test_new", "AssertionError: 少了一个")
    session.commit()

    features = collect_features(session, artifact_store, agent_run)

    assert features.message_shift is not None
    assert features.message_shift.compared == 1
    assert features.message_shift.changed == 1, "报错变了才说明走了新代码路径"


def test_without_a_noop_run_the_baseline_is_unavailable(
    world: World, session: Session, artifact_store: ArtifactStore
) -> None:
    """标 unavailable，不许拿零值糊 —— 零值会被 E6-T2 当成"报错一字没变"。"""
    agent_run = world.task_run()
    session.commit()

    features = collect_features(session, artifact_store, agent_run)

    assert features.message_shift is None
    assert "message_shift" in features.unavailable


def test_gold_patch_comes_from_raw_definition(
    world: World, session: Session, artifact_store: ArtifactStore
) -> None:
    """官方补丁要从 `raw_definition` 取，不能只认 `gold_patch_uri`。

    那一列是 `cli/queue.py` 拼出来的占位符，挖掘来的题上长成 `mined://…`，
    制品库不认这个 scheme。只认 URI 的话，`patch_overlap` 会对**所有真实题目**
    静默降级成 unavailable —— 而它恰好是 F2/F3/F4 最要紧的那一维。
    """
    agent_run = world.task_run()
    world.with_patches(
        agent_run,
        artifact_store,
        agent_diff="diff --git a/src/click/testing.py b/src/click/testing.py\n",
        gold_diff="diff --git a/src/click/utils.py b/src/click/utils.py\n",
    )
    session.commit()

    features = collect_features(session, artifact_store, agent_run)

    assert features.patch_overlap is not None, "官方补丁没取到"
    assert features.patch_overlap.agent_paths == ["src/click/testing.py"]
    assert features.patch_overlap.gold_paths == ["src/click/utils.py"]
    assert features.patch_overlap.hit_any is False, "改的文件和官方补丁没交集，这是 F2"


# ── 归因挂了不能影响判定（§12.4 最后一条）────────────────────


class BrokenStore:
    """一个怎么读都炸的制品库。用来验证特征取不到时归因不会整个塌掉。"""

    def get(self, key: str) -> bytes:
        raise OSError(f"制品库坏了：{key}")

    def __getattr__(self, name: str) -> object:
        raise OSError("制品库坏了")


def test_broken_artifact_store_degrades_instead_of_raising(world: World, session: Session) -> None:
    """制品库整个不通时，四组特征全标 unavailable —— 但不抛异常。"""
    agent_run = world.task_run()
    session.commit()

    features = collect_features(session, BrokenStore(), agent_run)  # type: ignore[arg-type]

    assert features.patch_overlap is None
    assert "patch_overlap" in features.unavailable


def test_attribution_never_writes_to_the_verdict_table(
    world: World, session: Session, artifact_store: ArtifactStore, engine: Engine
) -> None:
    """归因走一整遍，一条改 `evaluation_task_runs` 的 SQL 都不许发。

    §12.4 最后一条：判定和归因是两件事，归因挂了不能影响判定结果。
    与其测"炸了之后判定还在"，不如直接证明归因这条路**根本没有**改判定的能力 ——
    把发出去的 SQL 全录下来看一遍。
    """
    agent_run = world.task_run(agent_outcome=AgentOutcome.EMPTY_PATCH)
    session.commit()

    with count_queries(engine) as counter:
        save_rule_verdicts(session, rule_items(session))
        collect_features(session, artifact_store, agent_run)
        session.flush()

    writes = [
        sql
        for sql in counter.statements
        if "evaluation_task_runs" in sql.lower()
        and any(verb in sql.lower() for verb in ("update ", "insert into", "delete from"))
    ]
    assert writes == [], f"归因往判定表里写了：{writes}"


def test_a_crashing_classifier_writes_nothing(world: World, session: Session) -> None:
    """分类器炸了，一行归因都不许落，判定行一个字不许改。"""
    agent_run = world.task_run(agent_outcome=AgentOutcome.EMPTY_PATCH)
    session.commit()

    def run_with_a_broken_classifier() -> None:
        verdicts: list[tuple[int, RuleVerdict]] = []
        for row in load_facts(session):
            if row.task_run_id == agent_run:
                raise RuntimeError("分类器炸了")
            verdicts.append((row.task_run_id, classify(row.facts)))  # type: ignore[arg-type]
        save_rule_verdicts(session, verdicts)

    with pytest.raises(RuntimeError):
        run_with_a_broken_classifier()

    # 不能 rollback：conftest 的 session 夹具把整个测试包在一层外层事务里，
    # 这里一 rollback 连造好的数据一起没了。异常是在写库之前抛的，会话还是好的。
    session.expire_all()
    row = session.get(EvaluationTaskRun, agent_run)
    assert row is not None
    assert row.agent_outcome is AgentOutcome.EMPTY_PATCH, "判定被归因改动了"
    assert row.infra_outcome is InfraOutcome.SUCCESS
    assert session.scalars(select(FailureAttribution)).all() == []
