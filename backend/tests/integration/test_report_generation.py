"""报告聚合复用排行榜口径，并能把三种制品真实落库。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.domain.enums import AgentKind, ArtifactKind, ReportFormat
from app.infrastructure.models.artifact import Artifact
from app.infrastructure.models.attribution import ReportRecord
from app.infrastructure.models.evaluation import EvaluationTaskRun
from app.report.aggregate import build_report
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
