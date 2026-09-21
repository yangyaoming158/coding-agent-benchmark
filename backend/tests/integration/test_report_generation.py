"""报告聚合复用排行榜口径，并能把三种制品真实落库。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.domain.enums import (
    AgentKind,
    AgentOutcome,
    ArtifactKind,
    BenchmarkSetStatus,
    InfraOutcome,
    LifecycleStatus,
    ReportFormat,
)
from app.infrastructure.models.artifact import Artifact
from app.infrastructure.models.attribution import ReportRecord
from app.infrastructure.models.benchmark import BenchmarkSet, BenchmarkSetItem
from app.infrastructure.models.evaluation import EvaluationTaskRun
from app.report.aggregate import ReportInputError, build_report
from app.report.service import persist_report
from app.storage import LocalArtifactStore
from tests.integration.test_api_leaderboard import World


def test_build_report_reuses_eligible_rounds_and_preserves_cost_sources(
    session: Session, tmp_path: Path
) -> None:
    world = World(session)
    config_id = world.contestant("aider", "aider@deepseek-chat", kind=AgentKind.CLI)
    first = world.run(config_id, resolved=2, cost="0.30")
    second = world.run(config_id, resolved=4, cost="0.48")
    task_runs = session.scalars(
        sa.select(EvaluationTaskRun).where(
            EvaluationTaskRun.evaluation_run_id.in_([first.id, second.id])
        )
    ).all()
    for task_run in task_runs:
        task_run.cost_usd = Decimal("0.05")
        task_run.agent_duration_ms = 60_000
        task_run.total_duration_ms = 70_000
        task_run.completed_at = task_run.agent_started_at
    session.flush()
    host_metrics = tmp_path / "host.csv"
    host_metrics.write_text("at,cpu_pct,used_pct\n0,42.5,68.0\n1,71.0,73.5\n", encoding="utf-8")

    report = build_report(
        session,
        [first.id, second.id],
        generated_at=datetime(2026, 9, 20, tzinfo=UTC),
        host_metrics_csv=host_metrics,
    )

    assert report.scope == "COMPARISON"
    assert report.run_ids == [first.id, second.id]
    assert len(report.agents) == 1
    summary = report.agents[0]
    assert summary.run_count == 2
    assert summary.resolve_rate_mean == Decimal("0.5000")
    assert summary.resolve_rate_spread == Decimal("0.3334")
    assert summary.task_outcome_flip_count == 2
    assert summary.cost_reported_attempts == 12
    assert summary.cost_estimated_attempts == 0
    assert summary.cost_unavailable_attempts == 0
    assert summary.cost_distribution.p50_usd == Decimal("0.050000")
    assert report.performance.host_cpu.available
    assert report.performance.host_cpu_peak_pct == 71.0
    assert report.performance.host_memory.available
    assert report.performance.host_memory_peak_pct == 73.5
    assert any("只有 1 个真实 Agent" in warning for warning in report.warnings)


def test_persist_report_writes_real_artifact_and_report_rows(session: Session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    world = World(session)
    config_id = world.contestant("aider", "aider@deepseek-chat", kind=AgentKind.CLI)
    run = world.run(config_id, resolved=2)
    report = build_report(
        session,
        [run.id],
        generated_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    store = LocalArtifactStore(tmp_path)

    generated = persist_report(session, store, report)

    assert set(generated.artifacts) == set(ReportFormat)
    artifact_rows = session.scalars(sa.select(Artifact).where(Artifact.owner_id == run.id)).all()
    assert {row.kind for row in artifact_rows} == {
        ArtifactKind.REPORT_HTML,
        ArtifactKind.REPORT_MARKDOWN,
        ArtifactKind.REPORT_JSON,
    }
    record_rows = session.scalars(sa.select(ReportRecord)).all()
    assert {row.format for row in record_rows} == set(ReportFormat)
    assert all(row.run_ids == [run.id] for row in record_rows)
    assert all(store.exists(ref.key) for ref in generated.artifacts.values())


def test_mixed_datasets_keep_separate_scores_and_count_137_attempts(session: Session) -> None:
    world = World(session)
    config_id = world.contestant("aider", "aider@deepseek-chat", kind=AgentKind.CLI)
    chinese = world.run(config_id, resolved=2)
    official_set = BenchmarkSet(
        slug="swebench-verified-subset",
        version="v3",
        title="官方校准集",
        status=BenchmarkSetStatus.PUBLISHED,
        task_count=6,
    )
    session.add(official_set)
    session.flush()
    for index, task_id in enumerate(world.task_ids):
        session.add(
            BenchmarkSetItem(
                benchmark_set_id=official_set.id,
                benchmark_task_id=task_id,
                task_content_hash=f"{index:064d}",
                position=index,
            )
        )
    world.dataset = official_set
    official = world.run(config_id, resolved=4)
    first_task = session.scalar(
        sa.select(EvaluationTaskRun).where(
            EvaluationTaskRun.evaluation_run_id == chinese.id,
            EvaluationTaskRun.agent_outcome == AgentOutcome.UNRESOLVED,
        )
    )
    assert first_task is not None
    session.add(
        EvaluationTaskRun(
            evaluation_run_id=chinese.id,
            benchmark_task_id=first_task.benchmark_task_id,
            attempt_no=2,
            lifecycle_status=LifecycleStatus.COMPLETED,
            infra_outcome=InfraOutcome.AGENT_RUNTIME_ERROR,
            agent_outcome=AgentOutcome.UNRESOLVED,
            agent_started_at=chinese.created_at,
            exit_code=137,
            is_canonical=False,
        )
    )
    first_task.exit_code = 137
    first_task.infra_outcome = InfraOutcome.AGENT_TIMEOUT
    session.flush()

    report = build_report(session, [chinese.id, official.id])

    assert report.dataset is None
    assert [item.slug for item in report.datasets] == ["benchmark-dev", "swebench-verified-subset"]
    assert report.combined_task_count == 12
    assert len(report.agents) == 2
    assert any("失败归因与性能总量覆盖全部所选运行" in warning for warning in report.warnings)
    assert [source.resolved_counts for source in report.combined_agents[0].sources] == [[2], [4]]
    assert {row.dataset_label for row in report.task_results} == {
        "benchmark-dev@v1",
        "swebench-verified-subset@v3",
    }
    assert report.sigkill_without_oom_flag_count == 1
    assert [run.sigkill_without_oom_flag_count for run in report.runs] == [1, 0]

    official.protocol_version = "v1.3"
    with pytest.raises(ReportInputError, match="协议版本不同"):
        build_report(session, [chinese.id, official.id])
    official.protocol_version = "v1.2"
    official_set.slug = "benchmark-dev"
    with pytest.raises(ReportInputError, match="同一个数据集的不同版本"):
        build_report(session, [chinese.id, official.id])
