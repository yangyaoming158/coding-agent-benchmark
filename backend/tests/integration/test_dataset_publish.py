"""数据集快照发布与门禁在真库里的样子（E1-T6，`03-benchmark-spec.md` §7.5 + 协议 C-50）。

要一个真的 PostgreSQL：这里验的东西全都跨表 —— `benchmark_tasks` 的状态、
`benchmark_set_items` 的快照、`evaluation_runs` 的哨兵结果，三边必须一起对。

四件事最要紧：

1. **快照只收 `VALID`。** 库里现在混着人工终审否掉的 `INVALID`，收进去
   Oracle 必然掉出 100%，而排查的人会去翻判定引擎。
2. **门禁不达标就是不许发布。** 三种不达标各验一遍：Oracle 没到 100%、
   Noop 不是 0%、有题没拿到干净结果（那条最阴，解决率看着是对的）。
3. **门禁跑完之后改了快照，旧结果作废。** 否则"先建 set 再跑门禁"就真成了漏洞。
4. **已发布的版本一行都不改。** 隔离一道题只影响下一版，v1 原样保留 ——
   这是 §7.5"三周后重跑得到同一批题的同一版本"的全部意义。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.benchmark.dataset import (
    MANIFEST_DIGEST_KEY,
    DatasetError,
    drift,
    export_lines,
    freeze,
    gate_verdict,
    items_of,
    resolve_set,
    select_tasks,
    snapshot_digest,
)
from app.benchmark.hashing import compute_content_hash, to_bare_hex
from app.domain.enums import (
    AgentKind,
    AgentOutcome,
    BenchmarkSetStatus,
    EvaluationRunStatus,
    InfraOutcome,
    IssueLanguage,
    LifecycleStatus,
    TaskDifficulty,
    TaskValidationState,
)
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.benchmark import (
    BenchmarkSet,
    BenchmarkSetItem,
    BenchmarkTask,
    EnvironmentSpec,
    Repository,
)
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun
from tests.integration.factories import wipe

pytestmark = pytest.mark.db

DATASET_ID = "test-dev"


# ── 建数据 ──────────────────────────────────────────────────


def make_task(
    session: Session,
    repo: Repository,
    env: EnvironmentSpec,
    *,
    number: int,
    state: TaskValidationState = TaskValidationState.VALID,
    dataset_id: str = DATASET_ID,
) -> BenchmarkTask:
    """建一道题，`content_hash` 按 `raw_definition` 真算一遍。

    真算是必须的：`select_tasks()` 会重算一遍做校验，随便填一个哈希的话
    这里每道题都会被判成"对不上"，而那正是另一条测试要验的东西。
    """
    definition: dict[str, Any] = {
        "schema_version": "1.0",
        "task_id": f"acme__widget-{number}",
        "dataset_id": dataset_id,
        "issue_title": f"标题 {number}",
        "gold_patch": f"diff --git a/x{number}.py b/x{number}.py\n",
    }
    task = BenchmarkTask(
        task_id=f"acme__widget-{number}",
        repository_id=repo.id,
        environment_spec_id=env.id,
        base_commit=f"{number}" * 40,
        issue_title=f"标题 {number}",
        issue_body="正文",
        issue_language=IssueLanguage.ZH,
        fail_to_pass=["tests/test_a.py::test_new"],
        pass_to_pass=[],
        test_patch_uri="local://test.patch",
        test_patch_paths=["tests/test_a.py"],
        gold_patch_uri="local://gold.patch",
        difficulty=TaskDifficulty.EASY,
        validation_state=state,
        content_hash=to_bare_hex(compute_content_hash(definition)),
        raw_definition=definition,
    )
    session.add(task)
    session.flush()
    return task


@pytest.fixture
def world(session: Session) -> dict[str, Any]:
    """一个仓库、一个环境、三道 VALID + 一道 INVALID 的题、两个哨兵 Agent。"""
    wipe(session)
    repo = Repository(full_name="acme/widget", url="https://x/y", language="python")
    session.add(repo)
    session.flush()
    env = EnvironmentSpec(
        environment_id="acme__widget__py311",
        repository_id=repo.id,
        python_version="3.11",
        install_command="python -m pip install -e .",
        test_command="python -m pytest",
        test_report_path="report/junit.xml",
    )
    session.add(env)
    session.flush()

    valid = [make_task(session, repo, env, number=n) for n in (1, 2, 3)]
    rejected = make_task(session, repo, env, number=9, state=TaskValidationState.INVALID)

    configs: dict[str, int] = {}
    for name, kind in (("oracle", AgentKind.ORACLE), ("noop", AgentKind.NOOP)):
        agent = Agent(
            name=name, display_name=name, kind=kind, adapter_class=f"{name.title()}Runner"
        )
        session.add(agent)
        session.flush()
        config = AgentConfig(
            agent_id=agent.id,
            label=f"{name}-default",
            agent_version="1.0",
            model_name="none",
            config_hash=name.ljust(64, "0"),
        )
        session.add(config)
        session.flush()
        configs[name] = config.id

    session.flush()
    return {"repo": repo, "env": env, "valid": valid, "rejected": rejected, "configs": configs}


def stage(session: Session, world: dict[str, Any], *, slug: str = DATASET_ID) -> BenchmarkSet:
    """挑题 + 冻快照，返回那一版 DRAFT。"""
    selection = select_tasks(session, DATASET_ID)
    dataset = BenchmarkSet(slug=slug, version="v1", title="测试集", source_dataset_id=DATASET_ID)
    session.add(dataset)
    session.flush()
    freeze(session, dataset, selection.rows)
    return dataset


def run_sentinel(
    session: Session,
    world: dict[str, Any],
    dataset: BenchmarkSet,
    *,
    agent: str,
    resolved: set[str],
    digest: str | None = None,
    infra_broken: set[str] | None = None,
    status: EvaluationRunStatus = EvaluationRunStatus.COMPLETED,
) -> EvaluationRun:
    """伪造一次哨兵实验的结果。

    **不真跑评测**：门禁读的是 `evaluation_runs` 和 `evaluation_task_runs` 已经落库的
    那几个字段，伪造它们能把"门禁的判断规则"和"评测链路跑不跑得动"分开测。
    真跑一遍的证据在 PR 描述里，那是另一回事。
    """
    rows = items_of(session, dataset.id)
    broken = infra_broken or set()
    run = EvaluationRun(
        name=f"gate {agent}",
        benchmark_set_id=dataset.id,
        agent_config_id=world["configs"][agent],
        status=status,
        total_tasks=len(rows),
        resolved_count=len(resolved),
        manifest={MANIFEST_DIGEST_KEY: digest or snapshot_digest(rows)},
    )
    session.add(run)
    session.flush()

    for row in rows:
        if row.task_id in broken:
            # 平台自己出故障：合法组合是 FAILED / OOM_KILLED / NULL（协议 §4.3）
            lifecycle, infra, outcome = (
                LifecycleStatus.FAILED,
                InfraOutcome.OOM_KILLED,
                None,
            )
        elif row.task_id in resolved:
            lifecycle, infra, outcome = (
                LifecycleStatus.COMPLETED,
                InfraOutcome.SUCCESS,
                AgentOutcome.RESOLVED,
            )
        else:
            lifecycle, infra, outcome = (
                LifecycleStatus.COMPLETED,
                InfraOutcome.SUCCESS,
                AgentOutcome.EMPTY_PATCH,
            )
        session.add(
            EvaluationTaskRun(
                evaluation_run_id=run.id,
                benchmark_task_id=row.benchmark_task_id,
                attempt_no=1,
                lifecycle_status=lifecycle,
                infra_outcome=infra,
                agent_outcome=outcome,
                is_canonical=True,
                # 协议 C-69 / C-77：这四种组合的"区分条件"都要求 Agent 已经启动过，
                # `ck_evaluation_task_runs_legal_combination` 会当场拦下 NULL
                agent_started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
        )
    session.flush()
    return run


def pass_the_gate(session: Session, world: dict[str, Any], dataset: BenchmarkSet) -> None:
    """跑一次会通过的门禁：Oracle 全解、Noop 一道不解。"""
    everyone = {row.task_id for row in items_of(session, dataset.id)}
    run_sentinel(session, world, dataset, agent="oracle", resolved=everyone)
    run_sentinel(session, world, dataset, agent="noop", resolved=set())


# ── 挑题 ────────────────────────────────────────────────────


def test_only_valid_tasks_enter_the_snapshot(session: Session, world: dict[str, Any]) -> None:
    """人工终审否掉的题不进快照，而且要能说出被排除了几道、什么状态。"""
    selection = select_tasks(session, DATASET_ID)
    assert [r.task_id for r in selection.rows] == [
        "acme__widget-1",
        "acme__widget-2",
        "acme__widget-3",
    ]
    assert selection.excluded == {"INVALID": 1}
    assert selection.mismatched == ()


def test_quarantined_tasks_are_excluded_too(session: Session, world: dict[str, Any]) -> None:
    world["valid"][0].validation_state = TaskValidationState.QUARANTINED
    session.flush()
    selection = select_tasks(session, DATASET_ID)
    assert len(selection.rows) == 2
    assert selection.excluded == {"INVALID": 1, "QUARANTINED": 1}


def test_hash_mismatch_is_reported_not_frozen(session: Session, world: dict[str, Any]) -> None:
    """`content_hash` 那一列和 `raw_definition` 对不上，这道题不许进快照。

    冻一个假的"身份证"下去，以后 `verify` 报出来的漂移全是噪声。
    """
    world["valid"][1].content_hash = "f" * 64
    session.flush()
    selection = select_tasks(session, DATASET_ID)
    assert len(selection.rows) == 2
    assert len(selection.mismatched) == 1
    assert selection.mismatched[0].task_id == "acme__widget-2"
    assert selection.mismatched[0].stored == "f" * 64


# ── 冻快照 ──────────────────────────────────────────────────


def test_freeze_writes_items_and_derived_fields(session: Session, world: dict[str, Any]) -> None:
    dataset = stage(session, world)
    items = items_of(session, dataset.id)
    assert len(items) == 3
    assert dataset.task_count == 3
    assert dataset.snapshot_digest == to_bare_hex(snapshot_digest(items))
    # position 按 task_id 排序写死，"第几道题"是个确定的事实
    positions = session.execute(
        sa.select(BenchmarkSetItem.position).where(BenchmarkSetItem.benchmark_set_id == dataset.id)
    ).scalars()
    assert sorted(positions) == [1, 2, 3]


def test_freeze_stores_the_hash_of_that_moment(session: Session, world: dict[str, Any]) -> None:
    """快照存的是**当时**的哈希。之后题目被改了，快照那一列不跟着动。"""
    dataset = stage(session, world)
    frozen = items_of(session, dataset.id)[0].content_hash

    task = world["valid"][0]
    definition = dict(task.raw_definition)
    definition["issue_title"] = "改过的标题"
    task.raw_definition = definition
    task.content_hash = to_bare_hex(compute_content_hash(definition))
    session.flush()

    assert items_of(session, dataset.id)[0].content_hash == frozen
    assert task.content_hash != frozen


def test_freeze_refuses_a_published_set(session: Session, world: dict[str, Any]) -> None:
    """已发布的版本一行都不改。抛异常，不是静默跳过。"""
    dataset = stage(session, world)
    dataset.status = BenchmarkSetStatus.PUBLISHED
    session.flush()
    with pytest.raises(DatasetError, match="已发布的版本不许改题目清单"):
        freeze(session, dataset, select_tasks(session, DATASET_ID).rows)


# ── 门禁 ────────────────────────────────────────────────────


def test_gate_passes_when_oracle_is_100_and_noop_is_0(
    session: Session, world: dict[str, Any]
) -> None:
    dataset = stage(session, world)
    pass_the_gate(session, world, dataset)
    verdict = gate_verdict(session, dataset)
    assert verdict.ok, verdict.problems
    assert verdict.oracle is not None and verdict.oracle.resolved_count == 3
    assert verdict.noop is not None and verdict.noop.resolved_count == 0


def test_gate_rejects_when_no_sentinel_ran(session: Session, world: dict[str, Any]) -> None:
    """门禁根本没跑 ≠ 门禁通过。"""
    dataset = stage(session, world)
    verdict = gate_verdict(session, dataset)
    assert not verdict.ok
    assert len(verdict.problems) == 2
    assert all("还没跑" in p for p in verdict.problems)


def test_gate_rejects_oracle_below_100(session: Session, world: dict[str, Any]) -> None:
    """Oracle 掉出 100%：有坏题、判定引擎有 bug，或者补丁没打上（协议 C-50）。"""
    dataset = stage(session, world)
    run_sentinel(
        session,
        world,
        dataset,
        agent="oracle",
        resolved={"acme__widget-1", "acme__widget-2"},
    )
    run_sentinel(session, world, dataset, agent="noop", resolved=set())
    verdict = gate_verdict(session, dataset)
    assert not verdict.ok
    assert any("acme__widget-3" in p and "66.7%" in p for p in verdict.problems)


def test_gate_rejects_noop_above_0(session: Session, world: dict[str, Any]) -> None:
    """Noop 解出来了：那道题在修复前测试就通过，题库的下界不可信了。"""
    dataset = stage(session, world)
    everyone = {row.task_id for row in items_of(session, dataset.id)}
    run_sentinel(session, world, dataset, agent="oracle", resolved=everyone)
    run_sentinel(session, world, dataset, agent="noop", resolved={"acme__widget-2"})
    verdict = gate_verdict(session, dataset)
    assert not verdict.ok
    assert any("acme__widget-2" in p for p in verdict.problems)


def test_gate_rejects_when_a_task_never_got_a_clean_result(
    session: Session, world: dict[str, Any]
) -> None:
    """**这条是门禁第三条检查存在的理由。**

    一道题因为平台故障没跑成，它同样不是 RESOLVED —— Noop 那边的"0%"就这么被
    凑出来了。只查解决率的话门禁会放行，而那道题根本没验过。
    """
    dataset = stage(session, world)
    everyone = {row.task_id for row in items_of(session, dataset.id)}
    run_sentinel(
        session,
        world,
        dataset,
        agent="oracle",
        resolved=everyone - {"acme__widget-3"},
        infra_broken={"acme__widget-3"},
    )
    run_sentinel(
        session,
        world,
        dataset,
        agent="noop",
        resolved=set(),
        infra_broken={"acme__widget-3"},
    )
    verdict = gate_verdict(session, dataset)
    assert not verdict.ok
    # Noop 的解决率仍然是 0/3，但它一样被拦下
    assert verdict.noop is not None and verdict.noop.resolved_count == 0
    assert any("没拿到干净的结果" in p and "acme__widget-3" in p for p in verdict.problems)


def test_gate_rejects_an_unfinished_run(session: Session, world: dict[str, Any]) -> None:
    dataset = stage(session, world)
    everyone = {row.task_id for row in items_of(session, dataset.id)}
    run_sentinel(
        session,
        world,
        dataset,
        agent="oracle",
        resolved=everyone,
        status=EvaluationRunStatus.RUNNING,
    )
    run_sentinel(session, world, dataset, agent="noop", resolved=set())
    verdict = gate_verdict(session, dataset)
    assert not verdict.ok
    assert any("RUNNING" in p for p in verdict.problems)


def test_changing_the_snapshot_invalidates_the_gate(
    session: Session, world: dict[str, Any]
) -> None:
    """门禁跑完之后往快照里加一道题，旧的门禁结果作废。

    没有这一条，"先建 set 再跑门禁"就真成了漏洞：跑一份干净的小集合过门禁，
    再把坏题塞进去发布。
    """
    dataset = stage(session, world)
    pass_the_gate(session, world, dataset)
    assert gate_verdict(session, dataset).ok

    world["rejected"].validation_state = TaskValidationState.VALID
    session.flush()
    freeze(session, dataset, select_tasks(session, DATASET_ID).rows)

    verdict = gate_verdict(session, dataset)
    assert not verdict.ok
    assert all("还没跑" in p for p in verdict.problems)


def test_tampering_with_items_directly_is_caught(session: Session, world: dict[str, Any]) -> None:
    """绕过 `freeze()` 直接删一行 items，摘要就对不上了。"""
    dataset = stage(session, world)
    pass_the_gate(session, world, dataset)
    session.execute(
        sa.delete(BenchmarkSetItem).where(
            BenchmarkSetItem.benchmark_set_id == dataset.id,
            BenchmarkSetItem.position == 3,
        )
    )
    session.flush()
    verdict = gate_verdict(session, dataset)
    assert not verdict.ok
    assert any("快照摘要对不上" in p for p in verdict.problems)


def test_gate_rejects_an_empty_snapshot(session: Session, world: dict[str, Any]) -> None:
    dataset = BenchmarkSet(slug="empty", version="v1", title="空的")
    session.add(dataset)
    session.flush()
    verdict = gate_verdict(session, dataset)
    assert not verdict.ok
    assert "快照是空的" in verdict.problems[0]


# ── 挑版本 ──────────────────────────────────────────────────


def test_resolve_set_prefers_published(session: Session, world: dict[str, Any]) -> None:
    """不给版本号时优先拿已发布的那一版 —— 正式跑实验就该用过了门禁的。"""
    v1 = stage(session, world)
    v1.status = BenchmarkSetStatus.PUBLISHED
    v2 = BenchmarkSet(slug=DATASET_ID, version="v2", title="草稿", task_count=3)
    session.add(v2)
    session.flush()

    chosen, note = resolve_set(session, DATASET_ID)
    assert chosen.version == "v1"
    assert note is None


def test_resolve_set_falls_back_to_draft_but_says_so(
    session: Session, world: dict[str, Any]
) -> None:
    """一版都没发布时退回草稿，但**必须说出来** —— 悄悄拿没过门禁的题跑，
    结果会被当成正式数字。"""
    stage(session, world)
    chosen, note = resolve_set(session, DATASET_ID)
    assert chosen.version == "v1"
    assert note is not None and "没过门禁" in note


def test_resolve_set_reports_unknown_version(session: Session, world: dict[str, Any]) -> None:
    stage(session, world)
    with pytest.raises(DatasetError, match="没有 v7 这一版"):
        resolve_set(session, DATASET_ID, "v7")


# ── 漂移与隔离 ──────────────────────────────────────────────


def test_drift_is_clean_right_after_freezing(session: Session, world: dict[str, Any]) -> None:
    dataset = stage(session, world)
    assert drift(session, dataset.id).clean


def test_drift_reports_a_changed_task(session: Session, world: dict[str, Any]) -> None:
    dataset = stage(session, world)
    task = world["valid"][0]
    definition = dict(task.raw_definition)
    definition["issue_body"] = "改过了"
    task.raw_definition = definition
    task.content_hash = to_bare_hex(compute_content_hash(definition))
    session.flush()

    result = drift(session, dataset.id)
    assert result.changed == ("acme__widget-1",)
    assert result.quarantined == ()


def test_quarantine_shows_up_as_drift_but_keeps_the_snapshot(
    session: Session, world: dict[str, Any]
) -> None:
    """**§7.4 那句话在这里落地。**

    隔离一道题：已发布的 v1 一行都不改（`items` 还是 3 条），漂移检查如实报出来，
    而下一版会自动排除它。"历史版本不受影响"只有这一种读法说得通。
    """
    v1 = stage(session, world)
    pass_the_gate(session, world, v1)
    v1.status = BenchmarkSetStatus.PUBLISHED
    v1.published_at = datetime.now(UTC)
    session.flush()

    world["valid"][2].validation_state = TaskValidationState.QUARANTINED
    session.flush()

    # v1 的快照一行不动
    assert len(items_of(session, v1.id)) == 3
    assert v1.task_count == 3
    result = drift(session, v1.id)
    assert result.quarantined == ("acme__widget-3",)
    assert result.changed == ()

    # 下一版自动少一道
    v2 = BenchmarkSet(slug=DATASET_ID, version="v2", title="测试集", source_dataset_id=DATASET_ID)
    session.add(v2)
    session.flush()
    freeze(session, v2, select_tasks(session, DATASET_ID).rows)
    assert [row.task_id for row in items_of(session, v2.id)] == [
        "acme__widget-1",
        "acme__widget-2",
    ]
    assert v2.snapshot_digest != v1.snapshot_digest


# ── 导出 ────────────────────────────────────────────────────


def test_export_is_byte_stable(session: Session, world: dict[str, Any]) -> None:
    """同一份快照导出两次必须逐字节相同，否则 `dataset_sha256` 这个指纹没有意义。"""
    dataset = stage(session, world)
    rows = items_of(session, dataset.id)
    first = export_lines(session, rows)
    second = export_lines(session, list(reversed(rows)))
    assert first == second
    assert len(first) == 3
    assert json.loads(first[0])["task_id"] == "acme__widget-1"


def test_resolve_set_prefers_draft_for_the_publish_side(
    session: Session, world: dict[str, Any]
) -> None:
    """**`gate` / `publish` 要挑草稿，不是已发布的那一版。**

    反过来的话，v1 发布之后 `gate --slug X` 会解析到 v1、然后说"已经是 PUBLISHED，
    不用再跑门禁"，而人明明是想给刚 stage 出来的 v2 跑门禁。
    """
    v1 = stage(session, world)
    v1.status = BenchmarkSetStatus.PUBLISHED
    v2 = BenchmarkSet(slug=DATASET_ID, version="v2", title="草稿", task_count=2)
    session.add(v2)
    session.flush()

    consumer, _ = resolve_set(session, DATASET_ID)
    assert consumer.version == "v1"

    publisher, _ = resolve_set(session, DATASET_ID, prefer_draft=True)
    assert publisher.version == "v2"


def test_publish_side_falls_back_to_published_when_no_draft(
    session: Session, world: dict[str, Any]
) -> None:
    """没有草稿时发布端退回已发布的那一版 —— `publish` 才好说"已经发布过了"。"""
    v1 = stage(session, world)
    v1.status = BenchmarkSetStatus.PUBLISHED
    session.flush()
    chosen, _ = resolve_set(session, DATASET_ID, prefer_draft=True)
    assert chosen.version == "v1"
