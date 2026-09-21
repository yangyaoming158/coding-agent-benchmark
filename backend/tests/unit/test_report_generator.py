"""E10-T3 报告三种格式和制品登记。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.domain.enums import ArtifactKind, ReportFormat
from app.infrastructure.models.artifact import Artifact
from app.infrastructure.models.attribution import ReportRecord
from app.report.models import (
    AgentSummary,
    Availability,
    ConcurrencyMetric,
    CostDistribution,
    DatasetInfo,
    FailureCase,
    FailureSummary,
    PerformanceSummary,
    ProjectionSummary,
    ReportData,
    RunSummary,
    TaskResult,
)
from app.report.render import render_html, render_json, render_markdown
from app.report.service import persist_report
from app.storage import LocalArtifactStore

NOW = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)


def sample_report() -> ReportData:
    """造一份带费用缺失、制品链接和 HTML 特殊字符的完整报告。"""
    return ReportData(
        generated_at=NOW,
        title="A&B <pilot>",
        scope="COMPARISON",
        run_ids=[125, 126],
        dataset=DatasetInfo(
            id=4,
            slug="benchmark-cn-v1",
            version="v2",
            title="中文集",
            task_count=41,
            snapshot_digest="a" * 64,
        ),
        warnings=["只有 2 个真实 Agent；费用不能当作完整总成本。"],
        runs=[
            RunSummary(
                id=125,
                name="pilot-1",
                agent_config_id=8,
                agent_label="aider",
                agent_name="aider",
                agent_kind="CLI",
                protocol_version="v1.2",
                status="COMPLETED",
                total_tasks=41,
                completed_tasks=41,
                resolved_count=8,
                strict_resolve_rate=Decimal("0.1951"),
                effective_resolve_rate=Decimal("0.20"),
                infra_failure_count=1,
                retry_count=2,
                recovered_infra_failure_count=1,
                total_cost_usd=Decimal("1.25"),
                total_tokens=1234,
                makespan_ms=600_000,
                external_wait_ms=0,
                dirty=False,
                excluded_reason=None,
            )
        ],
        agents=[
            AgentSummary(
                agent_config_id=8,
                label="aider",
                agent_name="aider",
                model_name="deepseek-chat",
                protocol_version="v1.2",
                run_ids=[125, 126],
                run_count=2,
                tasks_per_run=41,
                resolve_rate_mean=Decimal("0.20"),
                resolve_rate_min=Decimal("0.15"),
                resolve_rate_max=Decimal("0.25"),
                resolve_rate_spread=Decimal("0.10"),
                effective_resolve_rate_mean=Decimal("0.21"),
                task_outcome_flip_count=3,
                task_outcome_flip_rate=Decimal("0.0732"),
                cost_usd_total=Decimal("1.25"),
                # 12 次报不出成本：每题成本是下界（1.25 / 82），不是 None
                cost_per_task=Decimal("0.0152"),
                cost_lower_bound=True,
                cost_reported_attempts=20,
                cost_estimated_attempts=50,
                cost_unavailable_attempts=12,
                cost_distribution=CostDistribution(
                    known_tasks=29,
                    unavailable_tasks=12,
                    p50_usd=Decimal("0.031"),
                    p95_usd=Decimal("0.088"),
                ),
                total_tokens=1234,
                tokens_per_task=30,
                makespan_ms_mean=600_000,
                infra_failure_total=1,
                infra_failure_rate=Decimal("0.0122"),
                retry_total=2,
                facets={},
            )
        ],
        task_results=[
            TaskResult(
                task_run_id=501,
                run_id=125,
                agent_label="aider",
                task_id="demo-1",
                issue_title="escape <script>alert(1)</script>",
                lifecycle_status="COMPLETED",
                infra_outcome="SUCCESS",
                agent_outcome="UNRESOLVED",
                cost_source="unavailable",
                cost_usd=None,
                patch_url="http://localhost:8000/api/task-runs/501/artifacts/AGENT_NORMALIZED",
                log_url=None,
                trajectory_url=None,
            )
        ],
        performance=PerformanceSummary(
            batch_makespan_minutes=10.0,
            timings=[],
            concurrency=[ConcurrencyMetric(curve="in_flight", peak=4, p50=3, span_s=600)],
            concurrency_points=[],
            external_wait=Availability(available=False, reason="适配器未写入"),
            external_wait_ratio=None,
            host_cpu=Availability(available=False, reason="没有 CPU 采样"),
            host_cpu_peak_pct=None,
            host_memory=Availability(available=True),
            host_memory_peak_pct=72.5,
            projection=ProjectionSummary(
                target_runs=300,
                target_machine="16 vCPU / 32 GiB",
                agent_limit=12,
                sandbox_limit=8,
                worker_slots=12,
                overhead_ratio=0.201,
                projected_hours=5.5,
                bottleneck="agent",
                fits_six_hours=True,
            ),
        ),
        failures=FailureSummary(
            total_failures=1,
            attributed_failures=0,
            unattributed_failures=1,
            category_counts={},
            heatmap=[],
            top_cases=[
                FailureCase(
                    task_run_id=501,
                    run_id=125,
                    agent_label="aider",
                    task_id="demo-1",
                    issue_title="escape <script>alert(1)</script>",
                    infra_outcome="SUCCESS",
                    agent_outcome="UNRESOLVED",
                    category=None,
                    attribution_status=None,
                    reasoning_zh=None,
                    patch_url="http://localhost:8000/api/task-runs/501/artifacts/AGENT_NORMALIZED",
                    log_url=None,
                    trajectory_url=None,
                )
            ],
            llm_attribution=Availability(available=False, reason="E6-T2 未完成"),
            review_accuracy=Availability(available=False, reason="没有盲检"),
            review_accuracy_value=None,
            kappa=Availability(available=False, reason="没有双人标注"),
            kappa_value=None,
        ),
    )


def test_three_formats_share_semantics_and_keep_missing_cost_visible() -> None:
    report = sample_report()

    body = json.loads(render_json(report))
    markdown = render_markdown(report)
    html = render_html(report)

    assert body["schema_version"] == "1.0"
    assert body["agents"][0]["cost_per_task"] == "0.0152"
    assert body["agents"][0]["cost_lower_bound"] is True
    for output in (markdown, html):
        assert "reported 20 / estimated 50 / unavailable 12" in output
        # 下界要带 "≥"，不能长得和完整成本一样
        assert "≥ $0.0152" in output
        assert "不可用" in output
        assert "$0.0000" not in output
        assert "逐题翻转率" in output
        assert "每题结果与制品" in output
        assert "16 vCPU / 32 GiB" in output


def test_html_is_self_contained_and_escapes_untrusted_text() -> None:
    html = render_html(sample_report())

    assert "<style>" in html
    assert "<svg" in html or "没有同时具备费用与解决率的数据" in html
    assert "<script src=" not in html
    assert "A&amp;B &lt;pilot&gt;" in html
    assert "escape &lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert 'href="http://localhost:8000/api/task-runs/501/artifacts/AGENT_NORMALIZED"' in html


class FakeSession:
    """只实现服务需要的 add/flush，用来验证写出的 ORM 行。"""

    def __init__(self) -> None:
        self.rows: list[Any] = []
        self.next_id = 1

    def add(self, row: Any) -> None:
        self.rows.append(row)

    def flush(self) -> None:
        row = self.rows[-1]
        if getattr(row, "id", None) is None:
            row.id = self.next_id
            self.next_id += 1


def test_persist_report_writes_and_registers_all_three_formats(tmp_path) -> None:
    session = FakeSession()
    store = LocalArtifactStore(tmp_path)

    result = persist_report(session, store, sample_report())  # type: ignore[arg-type]

    assert set(result.artifacts) == set(ReportFormat)
    assert all(store.exists(ref.key) for ref in result.artifacts.values())
    artifacts = [row for row in session.rows if isinstance(row, Artifact)]
    records = [row for row in session.rows if isinstance(row, ReportRecord)]
    assert {row.kind for row in artifacts} == {
        ArtifactKind.REPORT_HTML,
        ArtifactKind.REPORT_MARKDOWN,
        ArtifactKind.REPORT_JSON,
    }
    assert {row.format for row in records} == set(ReportFormat)
    assert all(row.run_ids == [125, 126] for row in records)
    assert all(row.params["schema_version"] == "1.0" for row in records)
