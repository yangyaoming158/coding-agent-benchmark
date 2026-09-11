"""运行 Manifest 落库与重放（E5-T4）。

**要真数据库，不要 Docker。** 这一层回答四个问题：

1. 建实验时 manifest / dirty / protocol_version 三样有没有真写进去；
2. 协议 C-27（工作区不干净拒绝启动）拦不拦得住，`--allow-dirty` 放行之后标没标；
3. 同一批题建两次，manifest 是不是只在允许不同的键上有差异（任务卡的 AC）；
4. `replay` 能不能从 manifest 重建一次等价运行，世界变了拦不拦得住。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import cli.experiment as experiment_cli
from app.benchmark.dataset import SnapshotRow, freeze, items_of, snapshot_digest
from app.domain.protocol import PROTOCOL_VERSION
from app.evaluation import manifest as manifest_mod
from app.evaluation.manifest import (
    ProvenanceError,
    build_manifest,
    collect_provenance,
    diff_manifests,
)
from app.evaluation.orchestrator import OrchestrationError, create_runs
from app.infrastructure.db import create_session_factory
from app.infrastructure.models.benchmark import BenchmarkSet, BenchmarkTask, EnvironmentSpec
from app.infrastructure.models.evaluation import EvaluationRun
from app.sandbox.container import ImageNotFoundError
from cli.images import _digests_in_run_manifests
from tests.integration.factories import Seeded, seed_minimal, wipe

pytestmark = pytest.mark.db

CLEAN_SHA = "c0ffee" * 6 + "abcd"


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
        # 题真冻进 benchmark_set_items：`replay` 会现算一遍快照摘要去比，
        # 不冻的话摘要算的是空清单，重放第一步就说"数据集摘要对不上"
        dataset = session.get(BenchmarkSet, result.benchmark_set_id)
        assert dataset is not None
        freeze(session, dataset, items_to_freeze(session, result))
        # 镜像 digest 表要有东西可采
        env = session.execute(sa.select(EnvironmentSpec)).scalar_one()
        env.image_tag = "bench-env:golden__textkit__py311"
        env.image_digest = "sha256:" + "f" * 64
        session.commit()
        return result


def items_to_freeze(session: Session, seeded: Seeded) -> list[SnapshotRow]:
    rows = session.execute(
        sa.select(BenchmarkTask.id, BenchmarkTask.task_id, BenchmarkTask.content_hash).where(
            BenchmarkTask.id.in_(seeded.task_ids)
        )
    ).all()
    return [
        SnapshotRow(benchmark_task_id=row[0], task_id=row[1], content_hash=row[2]) for row in rows
    ]


def live_digest(session: Session, seeded: Seeded) -> str:
    """现在库里这一版快照的摘要 —— `replay` 校验时算的就是它。"""
    return snapshot_digest(items_of(session, seeded.benchmark_set_id))


@pytest.fixture(autouse=True)
def fixed_world(monkeypatch: pytest.MonkeyPatch) -> None:
    """git 状态和主机信息都是外部事实，测试里钉死。

    不钉的话这一组用例在脏工作区（也就是开发时的每一刻）会集体变红，
    而它们要验的东西和工作区干不干净没关系。
    """
    monkeypatch.setattr(manifest_mod, "git_state", lambda: (CLEAN_SHA, False))
    monkeypatch.setattr(manifest_mod, "host_facts", lambda: None)
    # 镜像在不在是 docker 那边的事实。默认当作"在"，
    # 专门验"镜像没了"的那条用例自己改回来
    monkeypatch.setattr(experiment_cli, "inspect_image", lambda ref: None)


def provenance(session: Session, seeded: Seeded, **kwargs: object) -> object:
    return collect_provenance(
        session,
        benchmark_set_id=seeded.benchmark_set_id,
        snapshot_digest=live_digest(session, seeded),
        agent_config_id=seeded.agent_config_id,
        task_ids=kwargs.pop("task_ids", seeded.task_ids),  # type: ignore[arg-type]
        agent_concurrency=10,
        sandbox_concurrency=5,
        job_max_attempts=3,
        **kwargs,  # type: ignore[arg-type]
    )


# ── 落库 ────────────────────────────────────────────────────


def test_creating_a_run_writes_the_whole_manifest(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    """E5-T4 之前 `cli.experiment start` 建出来的 manifest 是空的。现在建不出空的。"""
    with factory() as session:
        (run,) = create_runs(
            session,
            name="落库",
            task_ids=seeded.task_ids,
            provenance=provenance(session, seeded),  # type: ignore[arg-type]
        )
        session.commit()
        run_id = run.id

    with factory() as session:
        row = session.get(EvaluationRun, run_id)
        assert row is not None
        assert row.manifest["harness_git_sha"] == CLEAN_SHA
        assert row.manifest["dataset_snapshot_digest"].startswith("sha256:")
        assert row.manifest["dataset"]["slug"] == "golden"
        assert row.manifest["agent"]["label"] == "mock-default"
        # 镜像 digest 表从 environment_specs 采，按 environment_id 排序
        assert row.manifest["images"] == {
            "golden__textkit__py311": {
                "tag": "bench-env:golden__textkit__py311",
                "digest": "sha256:" + "f" * 64,
            }
        }
        # 协议 C-67：创建时写入。显式写而不是靠列默认值
        assert row.protocol_version == PROTOCOL_VERSION
        assert row.manifest["protocol_version"] == PROTOCOL_VERSION
        assert row.dirty is False


def test_row_and_manifest_cannot_disagree(factory: sessionmaker[Session], seeded: Seeded) -> None:
    """并发数这些既在列上又在 manifest 里，只能有一个来源。"""
    with factory() as session:
        (run,) = create_runs(
            session,
            name="一致",
            task_ids=seeded.task_ids,
            provenance=provenance(session, seeded),  # type: ignore[arg-type]
        )
        session.commit()
        assert run.agent_concurrency == run.manifest["limits"]["agent_concurrency"] == 10
        assert run.sandbox_concurrency == run.manifest["limits"]["sandbox_concurrency"] == 5
        assert run.total_tasks == run.manifest["dataset"]["selected_task_count"] == 3


def test_provenance_and_task_list_must_come_from_the_same_pick(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    """凭证记 3 道、实际投 2 道 —— 这种运行的 manifest 是在说谎，不许建。"""
    with factory() as session:
        prov = provenance(session, seeded)
        with pytest.raises(OrchestrationError, match="对不上"):
            create_runs(
                session,
                name="对不上",
                task_ids=seeded.task_ids[:2],
                provenance=prov,  # type: ignore[arg-type]
            )


def test_a_subset_run_records_which_tasks_it_ran(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    """`--task` 只投两道时，光看快照摘要重放不出同一批题。"""
    with factory() as session:
        picked = seeded.task_ids[:2]
        prov = provenance(session, seeded, task_ids=picked)
        (run,) = create_runs(session, name="子集", task_ids=picked, provenance=prov)  # type: ignore[arg-type]
        session.commit()
        assert run.manifest["dataset"]["selected_task_count"] == 2
        assert run.manifest["dataset"]["snapshot_task_count"] == 3
        assert run.manifest["dataset"]["selected_task_ids"] == [
            "bench-golden__textkit-1",
            "bench-golden__textkit-2",
        ]


# ── 协议 C-27 / C-28 ────────────────────────────────────────


def test_a_dirty_worktree_is_refused(
    factory: sessionmaker[Session], seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """协议 C-27：工作区有未提交改动时，记下来的 sha 代表不了这份代码。"""
    monkeypatch.setattr(manifest_mod, "git_state", lambda: ("abc123", True))
    with factory() as session, pytest.raises(ProvenanceError, match="C-27"):
        provenance(session, seeded)


def test_allow_dirty_lets_it_through_but_marks_it(
    factory: sessionmaker[Session], seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """协议 C-28：放行，但库里和 manifest 里都如实标着，不得进排行榜。"""
    monkeypatch.setattr(manifest_mod, "git_state", lambda: ("abc123", True))
    with factory() as session:
        prov = provenance(session, seeded, allow_dirty=True)
        (run,) = create_runs(session, name="脏的", task_ids=seeded.task_ids, provenance=prov)  # type: ignore[arg-type]
        session.commit()
        assert run.dirty is True
        assert run.manifest["dirty"] is True


# ── 任务卡的 AC：两次运行只在时间戳上不同 ──────────────────


def test_two_runs_of_the_same_thing_are_equivalent(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    """同一批题、同一个 Agent 建两次，必须相同的字段逐字相同。

    这条就是任务卡那句"两次运行的 manifest diff 只在时间戳上不同"。
    注意它**不等于**"两次结果会一样" —— 协议 C-73 写的是测试执行的可复现性
    是目标不是保证，逐实例一致率是 MET-01 的口径（E10-T5）。
    """
    with factory() as session:
        (first,) = create_runs(
            session,
            name="第一次",
            task_ids=seeded.task_ids,
            provenance=provenance(session, seeded),  # type: ignore[arg-type]
        )
        (second,) = create_runs(
            session,
            name="第二次",
            task_ids=seeded.task_ids,
            provenance=provenance(session, seeded),  # type: ignore[arg-type]
        )
        session.commit()
        result = diff_manifests(first.manifest, second.manifest)

    assert result.equivalent, [item.path for item in result.pinned]
    # 只有建的时刻可能不同（host 在这一组里固定成 None）
    assert {item.path for item in result.volatile} <= {"created_at"}


def test_rounds_share_one_manifest(factory: sessionmaker[Session], seeded: Seeded) -> None:
    """`--rounds 3` 建的三个实验，输入条件完全一样（协议 C-55 的多轮取样）。"""
    with factory() as session:
        runs = create_runs(
            session,
            name="三轮",
            task_ids=seeded.task_ids,
            provenance=provenance(session, seeded),  # type: ignore[arg-type]
            rounds=3,
        )
        session.commit()
        assert len(runs) == 3
        for other in runs[1:]:
            assert diff_manifests(runs[0].manifest, other.manifest).pinned == ()
        # 每轮一份独立的字典，不是共用一个对象
        assert runs[0].manifest is not runs[1].manifest


# ── 镜像 digest 表（协议 C-36）──────────────────────────────


def test_an_unbuilt_environment_records_a_null_digest(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    """镜像还没建过时如实记 None，不编一个假 digest。"""
    with factory() as session:
        env = session.execute(sa.select(EnvironmentSpec)).scalar_one()
        env.image_digest = None
        session.commit()

        manifest = build_manifest(provenance(session, seeded))  # type: ignore[arg-type]
        assert manifest["images"]["golden__textkit__py311"]["digest"] is None


def test_gc_protects_digests_referenced_by_run_manifests(
    factory: sessionmaker[Session], seeded: Seeded
) -> None:
    """环境一重建，`environment_specs.image_digest` 就被新值覆盖。

    老实验 manifest 里钉的那个会立刻变成"没人引用"，下一次 gc 就删了 ——
    而那次实验的可复现性全靠它（协议 C-36）。
    """
    with factory() as session:
        create_runs(
            session,
            name="占着一个 digest",
            task_ids=seeded.task_ids,
            provenance=provenance(session, seeded),  # type: ignore[arg-type]
        )
        # 镜像重建：库里那一列换成新 digest
        env = session.execute(sa.select(EnvironmentSpec)).scalar_one()
        env.image_digest = "sha256:" + "e" * 64
        session.commit()

        protected = _digests_in_run_manifests(session)
        assert protected == {"sha256:" + "f" * 64}


# ── replay ──────────────────────────────────────────────────


def test_replay_rebuilds_an_equivalent_run(
    factory: sessionmaker[Session],
    seeded: Seeded,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """由 manifest 重建一次等价运行 —— 任务卡 AC 的前半句。"""
    with factory() as session:
        (source,) = create_runs(
            session,
            name="原版",
            task_ids=seeded.task_ids,
            provenance=provenance(session, seeded),  # type: ignore[arg-type]
        )
        session.commit()
        source_id = source.id

    assert experiment_cli.main(["replay", "--run", str(source_id)]) == 0
    out = capsys.readouterr().out
    assert "重建了 1 次等价运行" in out

    with factory() as session:
        runs = list(session.execute(sa.select(EvaluationRun).order_by(EvaluationRun.id)).scalars())
        assert len(runs) == 2
        replayed = runs[1]
        assert replayed.manifest["replay_of"] == source_id
        assert replayed.total_tasks == 3
        # 必须相同的字段逐字相同；差异只在允许不同的那几个键上
        result = diff_manifests(runs[0].manifest, replayed.manifest)
        assert result.equivalent, [item.path for item in result.pinned]
        assert {item.path for item in result.volatile} <= {"created_at", "replay_of"}


def test_replay_refuses_when_a_task_was_edited(
    factory: sessionmaker[Session],
    seeded: Seeded,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """题被改过就不是同一批题了，重放出来的不是等价运行。

    **光比快照摘要抓不到这种情况**：摘要算的是 `benchmark_set_items` 里**冻住**的
    哈希，题目改了它一动不动。而 Worker 跑题读的是 `benchmark_tasks` 里活的那一份。
    所以这里靠的是 E1-T6 的漂移检查。
    """
    with factory() as session:
        (source,) = create_runs(
            session,
            name="原版",
            task_ids=seeded.task_ids,
            provenance=provenance(session, seeded),  # type: ignore[arg-type]
        )
        session.commit()
        source_id = source.id
        # 手动改一道题的内容哈希 —— 等价于有人改了题
        task = session.execute(
            sa.select(BenchmarkTask).where(BenchmarkTask.task_id == "bench-golden__textkit-1")
        ).scalar_one()
        task.content_hash = "9" * 64
        session.commit()

    assert experiment_cli.main(["replay", "--run", str(source_id)]) == 1
    out = capsys.readouterr().out
    assert "内容变了 1 道：bench-golden__textkit-1" in out


def test_replay_refuses_when_the_image_is_gone(
    factory: sessionmaker[Session],
    seeded: Seeded,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """镜像被回收之后，那次实验的环境就重建不出来了（协议 C-36）。"""
    with factory() as session:
        (source,) = create_runs(
            session,
            name="原版",
            task_ids=seeded.task_ids,
            provenance=provenance(session, seeded),  # type: ignore[arg-type]
        )
        session.commit()
        source_id = source.id

    def gone(reference: str) -> None:
        raise ImageNotFoundError(reference)

    monkeypatch.setattr(experiment_cli, "inspect_image", gone)
    assert experiment_cli.main(["replay", "--run", str(source_id)]) == 1
    out = capsys.readouterr().out
    assert "镜像已经不在本地了" in out
    assert "bench-env@sha256:" + "f" * 64 in out


def test_replay_refuses_a_run_without_a_manifest(
    factory: sessionmaker[Session],
    seeded: Seeded,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """E5-T4 之前建的实验 manifest 是空的，事后补不出来。"""
    with factory() as session:
        run = EvaluationRun(
            name="老运行",
            benchmark_set_id=seeded.benchmark_set_id,
            agent_config_id=seeded.agent_config_id,
        )
        session.add(run)
        session.commit()
        run_id = run.id

    assert experiment_cli.main(["replay", "--run", str(run_id)]) == 1
    assert "没有 manifest" in capsys.readouterr().out


def test_replay_refuses_when_the_code_moved_on(
    factory: sessionmaker[Session],
    seeded: Seeded,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """harness 代码换了版本，重放出来的不是同一套逻辑。默认拦，明说才放。"""
    with factory() as session:
        (source,) = create_runs(
            session,
            name="原版",
            task_ids=seeded.task_ids,
            provenance=provenance(session, seeded),  # type: ignore[arg-type]
        )
        session.commit()
        source_id = source.id

    monkeypatch.setattr(manifest_mod, "git_state", lambda: ("d" * 40, False))
    assert experiment_cli.main(["replay", "--run", str(source_id)]) == 1
    out = capsys.readouterr().out
    assert "harness_git_sha" in out
    assert f"git checkout {CLEAN_SHA}" in out

    # 明说要用现在这版代码重放：放行，而且新运行如实记的是新 sha
    assert experiment_cli.main(["replay", "--run", str(source_id), "--allow-code-drift"]) == 0
    with factory() as session:
        replayed = list(
            session.execute(sa.select(EvaluationRun).order_by(EvaluationRun.id)).scalars()
        )[-1]
        assert replayed.manifest["harness_git_sha"] == "d" * 40


def test_manifest_command_compares_two_runs(
    factory: sessionmaker[Session],
    seeded: Seeded,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`manifest --run A --run B` 的返回码能直接当断言用。"""
    with factory() as session:
        runs = create_runs(
            session,
            name="两轮",
            task_ids=seeded.task_ids,
            provenance=provenance(session, seeded),  # type: ignore[arg-type]
            rounds=2,
        )
        session.commit()
        ids = [str(run.id) for run in runs]

    assert experiment_cli.main(["manifest", "--run", ids[0], "--run", ids[1]]) == 0
    out = capsys.readouterr().out
    assert "必须相同的字段：全部一致" in out

    assert experiment_cli.main(["manifest", "--run", ids[0]]) == 0
    assert "运行 manifest" in capsys.readouterr().out
