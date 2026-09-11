"""`python -m cli.dataset` 从头走到尾（E1-T6）。

上一个文件验的是规则本身，这个文件验的是**六个子命令串起来还对不对**：
冻快照 → 门禁没跑就拒绝发布 → 门禁过了才发布 → 指纹和导出真的落地 →
再发一次是幂等的 → 隔离一道题之后出新版本，而已发布的那一版一行不改。

## 为什么这里要真 commit

`cli.dataset` 自己开 engine、自己开 session，不吃测试的那层回滚事务。
所以这些用例是真写库的，收尾靠 `wipe()`。这也正是它们的价值 ——
"CLI 自己那条连接上事务提交了没有"是集成测试才能回答的问题。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.orm import Session

import cli.dataset as dataset_cli
from app.benchmark.dataset import MANIFEST_DIGEST_KEY, items_of, snapshot_digest
from app.benchmark.hashing import compute_content_hash, to_bare_hex
from app.domain.enums import (
    AgentOutcome,
    BenchmarkSetStatus,
    EvaluationRunStatus,
    InfraOutcome,
    LifecycleStatus,
    TaskValidationState,
)
from app.evaluation import manifest as manifest_mod
from app.infrastructure.db import create_session_factory, session_scope
from app.infrastructure.models.benchmark import BenchmarkSet, BenchmarkSetItem, BenchmarkTask
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun
from tests.integration.factories import wipe
from tests.integration.test_dataset_publish import DATASET_ID, make_task

pytestmark = pytest.mark.db


@pytest.fixture
def cli_world(engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """真写库的一套数据，外加把两个产物目录挪到 tmp。

    产物目录必须挪走：`publish` 会往 `datasets/manifests/` 写文件，
    跑一次测试就在仓库里留一份垃圾。
    """
    monkeypatch.setattr(dataset_cli, "MANIFEST_ROOT", tmp_path / "manifests")
    monkeypatch.setattr(dataset_cli, "EXPORT_ROOT", tmp_path / "exports")
    # 工作区干不干净是外部事实，测试里不能靠它。默认给一个干净的。
    #
    # 两处都要打：`publish` 自己读 git（写进指纹文件），`gate` 走
    # `collect_provenance()` 读 git（协议 C-27 的强制点在那里）。
    # 两个模块都是 `from ... import git_state`，名字在 import 时就绑死了，
    # 只打源头模块不起作用。
    monkeypatch.setattr(dataset_cli, "git_state", lambda: ("c0ffee" * 6 + "abcd", False))
    monkeypatch.setattr(manifest_mod, "git_state", lambda: ("c0ffee" * 6 + "abcd", False))
    # 建实验时会顺手记一句"跑在什么机器上"。那一项允许两次运行不同，
    # 测试里固定成 None，免得 docker 在不在把用例变成时灵时不灵的
    monkeypatch.setattr(manifest_mod, "host_facts", lambda: None)

    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        wipe(session)
        _seed(session)
    yield tmp_path
    with session_scope(factory) as session:
        wipe(session)


def _seed(session: Session) -> None:
    """一个仓库、一个环境、三道 VALID + 一道 INVALID、两个哨兵 Agent。"""
    from app.domain.enums import AgentKind
    from app.infrastructure.models.agent import Agent, AgentConfig
    from app.infrastructure.models.benchmark import EnvironmentSpec, Repository

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
    for number in (1, 2, 3):
        make_task(session, repo, env, number=number)
    make_task(session, repo, env, number=9, state=TaskValidationState.INVALID)

    for name, kind in (("oracle", AgentKind.ORACLE), ("noop", AgentKind.NOOP)):
        agent = Agent(
            name=name, display_name=name, kind=kind, adapter_class=f"{name.title()}Runner"
        )
        session.add(agent)
        session.flush()
        session.add(
            AgentConfig(
                agent_id=agent.id,
                label=f"{name}-default",
                agent_version="1.0",
                model_name="none",
                config_hash=name.ljust(64, "0"),
            )
        )
    session.flush()


def fake_gate(
    engine: Engine, *, version: str = "v1", noop_resolves: set[str] | None = None
) -> None:
    """把两次哨兵实验的结果直接写进库。

    真跑一遍要起几十个容器，那是交付时的证据，不是每次 CI 都该付的代价。
    这里要验的是"CLI 读到这些数字之后做什么决定"。
    """
    from datetime import UTC, datetime

    from app.infrastructure.models.agent import Agent, AgentConfig

    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        dataset = session.execute(
            sa.select(BenchmarkSet).where(BenchmarkSet.version == version)
        ).scalar_one()
        rows = items_of(session, dataset.id)
        digest = snapshot_digest(rows)
        for agent_name in ("oracle", "noop"):
            config = session.execute(
                sa.select(AgentConfig).join(Agent).where(Agent.name == agent_name)
            ).scalar_one()
            resolves = (
                {row.task_id for row in rows}
                if agent_name == "oracle"
                else (noop_resolves or set())
            )
            run = EvaluationRun(
                name=f"gate {agent_name}",
                benchmark_set_id=dataset.id,
                agent_config_id=config.id,
                status=EvaluationRunStatus.COMPLETED,
                total_tasks=len(rows),
                resolved_count=len(resolves),
                manifest={MANIFEST_DIGEST_KEY: digest},
            )
            session.add(run)
            session.flush()
            for row in rows:
                session.add(
                    EvaluationTaskRun(
                        evaluation_run_id=run.id,
                        benchmark_task_id=row.benchmark_task_id,
                        attempt_no=1,
                        lifecycle_status=LifecycleStatus.COMPLETED,
                        infra_outcome=InfraOutcome.SUCCESS,
                        agent_outcome=(
                            AgentOutcome.RESOLVED
                            if row.task_id in resolves
                            else AgentOutcome.EMPTY_PATCH
                        ),
                        is_canonical=True,
                        agent_started_at=datetime.now(UTC),
                        completed_at=datetime.now(UTC),
                    )
                )
        session.flush()


# ── 全流程 ──────────────────────────────────────────────────


def test_full_flow(cli_world: Path, engine: Engine, capsys: pytest.CaptureFixture[str]) -> None:
    """stage → 拒绝 → 门禁 → 发布 → 幂等 → 隔离 → 出新版本。

    写成一条而不是拆成七条：这些步骤有真实的先后依赖，拆开的话每一条都要把
    前面几步重跑一遍，跑得更慢、读起来也看不出流程。
    """
    factory = create_session_factory(engine)

    # ① 冻快照：只收 VALID，那道 INVALID 被挡在外面
    assert dataset_cli.main(["stage", "--dataset-id", DATASET_ID]) == 0
    out = capsys.readouterr().out
    assert "冻进快照        3 道题" in out
    assert "INVALID 1" in out
    with session_scope(factory) as session:
        dataset = session.execute(sa.select(BenchmarkSet)).scalar_one()
        assert (dataset.version, dataset.status) == ("v1", BenchmarkSetStatus.DRAFT)
        assert dataset.source_dataset_id == DATASET_ID
        assert dataset.task_count == 3
        assert dataset.published_at is None

    # ② 门禁没跑就发布 → 拒绝
    assert dataset_cli.main(["publish", "--slug", DATASET_ID]) == 1
    out = capsys.readouterr().out
    assert "不达标，拒绝发布" in out
    assert "还没跑" in out
    with session_scope(factory) as session:
        assert (
            session.execute(sa.select(BenchmarkSet.status)).scalar_one() is BenchmarkSetStatus.DRAFT
        )

    # ③ 门禁过了 → 发布，两个产物落地
    fake_gate(engine)
    assert dataset_cli.main(["publish", "--slug", DATASET_ID]) == 0
    out = capsys.readouterr().out
    assert "已发布" in out
    manifest_path = cli_world / "manifests" / f"{DATASET_ID}@v1.json"
    export_path = cli_world / "exports" / f"{DATASET_ID}@v1.jsonl"
    assert manifest_path.exists() and export_path.exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["task_count"] == 3
    assert manifest["dirty"] is False
    assert manifest["gate"]["oracle"]["resolve_rate"] == 1.0
    assert manifest["gate"]["noop"]["resolve_rate"] == 0.0
    assert len(manifest["tasks"]) == 3
    # 导出一行一道题，而且含 gold_patch（所以这个文件不入库）
    lines = export_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3
    assert "gold_patch" in json.loads(lines[0])

    with session_scope(factory) as session:
        dataset = session.execute(sa.select(BenchmarkSet)).scalar_one()
        assert dataset.status is BenchmarkSetStatus.PUBLISHED
        assert dataset.published_at is not None
        assert dataset.publish_evidence is not None
        assert dataset.publish_evidence["protocol_clause"] == "C-50"

    # ④ 再发一次是幂等的，不会重复发布
    assert dataset_cli.main(["publish", "--slug", DATASET_ID]) == 0
    assert "已经发布过了" in capsys.readouterr().out

    # ⑤ 题一道没变，再 stage 一次不该涨版本号
    assert dataset_cli.main(["stage", "--dataset-id", DATASET_ID]) == 0
    assert "不用出新版本" in capsys.readouterr().out
    with session_scope(factory) as session:
        assert session.execute(sa.select(sa.func.count()).select_from(BenchmarkSet)).scalar() == 1

    # ⑥ 隔离一道题：已发布的 v1 一行都不改
    assert dataset_cli.main(["quarantine", "--task", "acme__widget-3", "--reason", "复验挂了"]) == 0
    out = capsys.readouterr().out
    assert "VALID → QUARANTINED" in out
    assert "一行都不改" in out
    with session_scope(factory) as session:
        v1 = session.execute(sa.select(BenchmarkSet)).scalar_one()
        assert v1.task_count == 3
        assert len(items_of(session, v1.id)) == 3
        task = session.execute(
            sa.select(BenchmarkTask).where(BenchmarkTask.task_id == "acme__widget-3")
        ).scalar_one()
        assert task.validation_state is TaskValidationState.QUARANTINED
        assert task.quarantine is not None
        assert task.quarantine["reason"] == "复验挂了"
        assert task.quarantine["from_state"] == "VALID"
        # **理由不许写进 raw_definition。** 那一列是 TaskDefinition 的原文
        # （`extra="forbid"`），多一个键这道题就再也解析不回来；而且 content_hash
        # 算的就是它。2026-09-10 第一版真这么写过，隔离完 `cli.validate run` 当场崩。
        #
        # 判据用"哈希还对得上"而不是"少了某个键"：前者正是 `dataset stage` 会查的
        # 那一条，往 raw_definition 里加任何东西都会让它红。
        assert to_bare_hex(compute_content_hash(task.raw_definition)) == task.content_hash

    # ⑦ 漂移检查如实报出来
    assert dataset_cli.main(["verify", "--slug", DATASET_ID, "--version", "v1"]) == 1
    out = capsys.readouterr().out
    assert "快照摘要一致" in out
    assert "已被隔离" in out and "acme__widget-3" in out

    # ⑧ 出新版本：v2 自动少那道题，v1 还在
    assert dataset_cli.main(["stage", "--dataset-id", DATASET_ID]) == 0
    out = capsys.readouterr().out
    assert "新建 v2" in out
    assert "冻进快照        2 道题" in out
    assert "QUARANTINED 1" in out
    with session_scope(factory) as session:
        versions = {row.version: row for row in session.execute(sa.select(BenchmarkSet)).scalars()}
        assert set(versions) == {"v1", "v2"}
        assert versions["v1"].task_count == 3
        assert versions["v1"].status is BenchmarkSetStatus.PUBLISHED
        assert versions["v2"].task_count == 2
        assert versions["v2"].status is BenchmarkSetStatus.DRAFT
        assert versions["v1"].snapshot_digest != versions["v2"].snapshot_digest


# ── 单个命令的边角 ──────────────────────────────────────────


def test_publish_refuses_a_dirty_worktree(
    cli_world: Path,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """协议 C-27：工作区不干净时 `harness_git_sha` 代表不了代码状态。"""
    monkeypatch.setattr(dataset_cli, "git_state", lambda: ("abc123", True))
    assert dataset_cli.main(["stage", "--dataset-id", DATASET_ID]) == 0
    fake_gate(engine)
    capsys.readouterr()

    assert dataset_cli.main(["publish", "--slug", DATASET_ID]) == 1
    assert "工作区有未提交改动，拒绝发布" in capsys.readouterr().out

    # --allow-dirty 放行，但指纹里如实标着
    assert dataset_cli.main(["publish", "--slug", DATASET_ID, "--allow-dirty"]) == 0
    assert "dirty: true" in capsys.readouterr().out
    manifest = json.loads(
        (cli_world / "manifests" / f"{DATASET_ID}@v1.json").read_text(encoding="utf-8")
    )
    assert manifest["dirty"] is True


def test_publish_refuses_when_noop_solves_a_task(
    cli_world: Path, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """Noop 解出来了 = 有题在修复前测试就通过。协议 C-50 说不通过不许发布。"""
    assert dataset_cli.main(["stage", "--dataset-id", DATASET_ID]) == 0
    fake_gate(engine, noop_resolves={"acme__widget-2"})
    capsys.readouterr()

    assert dataset_cli.main(["publish", "--slug", DATASET_ID]) == 1
    out = capsys.readouterr().out
    assert "不达标，拒绝发布" in out
    assert "acme__widget-2" in out
    with session_scope(create_session_factory(engine)) as session:
        assert (
            session.execute(sa.select(BenchmarkSet.status)).scalar_one() is BenchmarkSetStatus.DRAFT
        )


def test_gate_refuses_to_double_queue(
    cli_world: Path, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """门禁还在跑的时候再投一遍，作业会翻倍。拦下来，除非明说 --force。"""
    assert dataset_cli.main(["stage", "--dataset-id", DATASET_ID]) == 0
    assert dataset_cli.main(["gate", "--slug", DATASET_ID]) == 0
    out = capsys.readouterr().out
    assert "oracle" in out and "noop" in out

    assert dataset_cli.main(["gate", "--slug", DATASET_ID]) == 1
    assert "还在跑" in capsys.readouterr().out

    with session_scope(create_session_factory(engine)) as session:
        runs = list(session.execute(sa.select(EvaluationRun)).scalars())
        assert len(runs) == 2
        # 题从快照里取，不是整张 benchmark_tasks 表 —— 那道 INVALID 不在里面
        assert {run.total_tasks for run in runs} == {3}
        assert all(run.manifest[MANIFEST_DIGEST_KEY].startswith("sha256:") for run in runs)


def test_gate_rejects_a_snapshot_changed_after_gating(
    cli_world: Path, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """门禁跑完之后往快照里塞题，旧结果作废 —— 否则这就是个绕过门禁的洞。"""
    assert dataset_cli.main(["stage", "--dataset-id", DATASET_ID]) == 0
    fake_gate(engine)

    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        # 把那道被终审否掉的题改成 VALID，再冻一次
        session.execute(
            sa.update(BenchmarkTask)
            .where(BenchmarkTask.task_id == "acme__widget-9")
            .values(validation_state=TaskValidationState.VALID)
        )
    assert dataset_cli.main(["stage", "--dataset-id", DATASET_ID]) == 0
    out = capsys.readouterr().out
    assert "摘要变了，之前跑过的门禁作废" in out

    assert dataset_cli.main(["publish", "--slug", DATASET_ID]) == 1
    assert "还没跑" in capsys.readouterr().out


def test_stage_refuses_when_content_hash_disagrees(
    cli_world: Path, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """`content_hash` 那一列和 `raw_definition` 对不上就整批拒绝，并点名。"""
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        session.execute(
            sa.update(BenchmarkTask)
            .where(BenchmarkTask.task_id == "acme__widget-2")
            .values(content_hash="f" * 64)
        )

    assert dataset_cli.main(["stage", "--dataset-id", DATASET_ID]) == 1
    out = capsys.readouterr().out
    assert "拒绝冻快照" in out
    assert "acme__widget-2" in out
    with session_scope(factory) as session:
        items = session.execute(sa.select(sa.func.count()).select_from(BenchmarkSetItem)).scalar()
        assert items == 0


def test_show_lists_versions(
    cli_world: Path, engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert dataset_cli.main(["stage", "--dataset-id", DATASET_ID]) == 0
    capsys.readouterr()
    assert dataset_cli.main(["show", "--slug", DATASET_ID, "--tasks"]) == 0
    out = capsys.readouterr().out
    assert f"{DATASET_ID}@v1" in out
    assert "acme__widget-1" in out
