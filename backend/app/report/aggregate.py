"""从已有评测事实生成报告数据。

本模块只负责查询与聚合，不负责 HTML 排版。解决率、阶段耗时和并发曲线优先
复用现有模块，避免报告悄悄形成第二套统计口径。
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from math import ceil
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.analytics import concurrency as concurrency_mod
from app.analytics import leaderboard, timing
from app.domain.enums import (
    AgentKind,
    AgentOutcome,
    ArtifactKind,
    ArtifactOwnerType,
    AttributionStage,
    CostSource,
    HumanReviewAction,
    PatchKind,
    ReportScope,
)
from app.domain.makespan import MakespanInputs, project
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.artifact import Artifact
from app.infrastructure.models.attribution import FailureAttribution, HumanReview
from app.infrastructure.models.benchmark import BenchmarkSet, BenchmarkTask
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun, PatchArtifact
from app.report.models import (
    AgentSummary,
    Availability,
    ConcurrencyMetric,
    ConcurrencyPoint,
    CostDistribution,
    DatasetInfo,
    FacetMetric,
    FailureCase,
    FailureCell,
    FailureSummary,
    PerformanceSummary,
    ProjectionSummary,
    ReportData,
    RunSummary,
    StageMetric,
    TaskResult,
    TimingSummary,
)

_RATE_PLACES = Decimal("0.0001")
_COST_PLACES = Decimal("0.000001")
_SENTINELS = {AgentKind.ORACLE, AgentKind.NOOP, AgentKind.MOCK}


class ReportInputError(ValueError):
    """选择的运行无法组成一份可比较报告。"""


def _rate(numerator: int, denominator: int) -> Decimal | None:
    if denominator <= 0:
        return None
    return (Decimal(numerator) / Decimal(denominator)).quantize(_RATE_PLACES)


def _decimal_percentile(values: Sequence[Decimal], fraction: float) -> Decimal | None:
    """费用分布使用和阶段耗时相同的最近秩百分位。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1].quantize(_COST_PLACES)


def _load_run_rows(
    session: Session, run_ids: Sequence[int]
) -> list[tuple[EvaluationRun, AgentConfig, Agent, BenchmarkSet]]:
    if not run_ids:
        raise ReportInputError("至少要选择一个实验")
    unique_ids = list(dict.fromkeys(run_ids))
    rows = session.execute(
        sa.select(EvaluationRun, AgentConfig, Agent, BenchmarkSet)
        .join(AgentConfig, AgentConfig.id == EvaluationRun.agent_config_id)
        .join(Agent, Agent.id == AgentConfig.agent_id)
        .join(BenchmarkSet, BenchmarkSet.id == EvaluationRun.benchmark_set_id)
        .where(EvaluationRun.id.in_(unique_ids))
        .order_by(EvaluationRun.id)
    ).all()
    found = {row[0].id for row in rows}
    missing = [run_id for run_id in unique_ids if run_id not in found]
    if missing:
        raise ReportInputError(f"找不到实验：{', '.join('#' + str(i) for i in missing)}")
    return [(row[0], row[1], row[2], row[3]) for row in rows]


def _validate_comparability(
    rows: Sequence[tuple[EvaluationRun, AgentConfig, Agent, BenchmarkSet]],
) -> BenchmarkSet:
    sets = {run.benchmark_set_id for run, _, _, _ in rows}
    if len(sets) != 1:
        raise ReportInputError("对比报告只能包含同一个 benchmark_set 版本的实验")
    protocols = {run.protocol_version for run, _, _, _ in rows}
    if len(protocols) != 1:
        raise ReportInputError("协议版本不同的实验不能放进同一份对比报告（C-59）")
    return rows[0][3]


def _run_summaries(
    rows: Sequence[tuple[EvaluationRun, AgentConfig, Agent, BenchmarkSet]],
) -> list[RunSummary]:
    return [
        RunSummary(
            id=run.id,
            name=run.name,
            agent_config_id=config.id,
            agent_label=config.label,
            agent_name=agent.name,
            agent_kind=agent.kind.value,
            protocol_version=run.protocol_version,
            status=run.status.value,
            total_tasks=run.total_tasks,
            completed_tasks=run.completed_tasks,
            resolved_count=run.resolved_count,
            strict_resolve_rate=run.strict_resolve_rate,
            effective_resolve_rate=run.effective_resolve_rate,
            infra_failure_count=run.infra_failure_count,
            retry_count=run.retry_count,
            recovered_infra_failure_count=run.recovered_infra_failure_count,
            total_cost_usd=run.total_cost_usd,
            total_tokens=run.total_tokens,
            makespan_ms=run.makespan_ms,
            external_wait_ms=run.external_wait_ms,
            dirty=run.dirty,
            excluded_reason=run.leaderboard_excluded_reason,
        )
        for run, config, agent, _ in rows
    ]


def _cost_distributions(session: Session, run_ids: Sequence[int]) -> dict[int, CostDistribution]:
    rows = session.execute(
        sa.select(
            EvaluationRun.agent_config_id,
            EvaluationTaskRun.evaluation_run_id,
            EvaluationTaskRun.benchmark_task_id,
            EvaluationTaskRun.cost_source,
            EvaluationTaskRun.cost_usd,
        )
        .join(EvaluationRun, EvaluationRun.id == EvaluationTaskRun.evaluation_run_id)
        .where(EvaluationTaskRun.evaluation_run_id.in_(list(run_ids)))
    ).all()
    grouped: dict[int, dict[tuple[int, int], list[tuple[CostSource | None, Decimal | None]]]] = (
        defaultdict(lambda: defaultdict(list))
    )
    for config_id, run_id, task_id, source, cost in rows:
        grouped[int(config_id)][(int(run_id), int(task_id))].append((source, cost))

    result: dict[int, CostDistribution] = {}
    for config_id, tasks in grouped.items():
        known: list[Decimal] = []
        unavailable = 0
        for attempts in tasks.values():
            if any(
                source in (None, CostSource.UNAVAILABLE) or cost is None
                for source, cost in attempts
            ):
                unavailable += 1
                continue
            known.append(sum((cost or Decimal(0) for _, cost in attempts), Decimal(0)))
        result[config_id] = CostDistribution(
            known_tasks=len(known),
            unavailable_tasks=unavailable,
            p50_usd=_decimal_percentile(known, 0.50),
            p95_usd=_decimal_percentile(known, 0.95),
        )
    return result


def _task_flips(session: Session, run_ids: Sequence[int]) -> dict[int, tuple[int, Decimal | None]]:
    rows = session.execute(
        sa.select(
            EvaluationRun.agent_config_id,
            EvaluationTaskRun.benchmark_task_id,
            EvaluationTaskRun.agent_outcome,
        )
        .join(EvaluationRun, EvaluationRun.id == EvaluationTaskRun.evaluation_run_id)
        .where(
            EvaluationTaskRun.evaluation_run_id.in_(list(run_ids)),
            EvaluationTaskRun.is_canonical.is_(True),
        )
    ).all()
    grouped: dict[int, dict[int, list[AgentOutcome | None]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for config_id, task_id, outcome in rows:
        grouped[int(config_id)][int(task_id)].append(outcome)
    result: dict[int, tuple[int, Decimal | None]] = {}
    for config_id, tasks in grouped.items():
        comparable = [outcomes for outcomes in tasks.values() if len(outcomes) > 1]
        flipped = sum(1 for outcomes in comparable if len(set(outcomes)) > 1)
        result[config_id] = (flipped, _rate(flipped, len(comparable)))
    return result


def _facets(
    session: Session, run_ids: Sequence[int], eligible: Sequence[leaderboard.EligibleRun]
) -> dict[tuple[int, str], dict[str, list[FacetMetric]]]:
    result: dict[tuple[int, str], dict[str, list[FacetMetric]]] = defaultdict(dict)
    for facet in leaderboard.LeaderboardFacet:
        counts = leaderboard.facet_counts(session, run_ids, facet=facet)
        rows = leaderboard.summarize(eligible, facets=counts)
        for row in rows:
            result[(row.agent_config_id, row.protocol_version)][facet.value] = [
                FacetMetric(
                    value=cell.value,
                    resolved=cell.resolved,
                    total=cell.total,
                    resolve_rate=cell.resolve_rate,
                )
                for cell in row.facets
            ]
    return result


def _agent_summaries(
    session: Session, dataset_id: int, selected_ids: Sequence[int]
) -> list[AgentSummary]:
    eligible = [
        run
        for run in leaderboard.eligible_runs(session, benchmark_set_id=dataset_id)
        if run.evaluation_run_id in selected_ids
    ]
    eligible_ids = [run.evaluation_run_id for run in eligible]
    costs = leaderboard.cost_sources(session, eligible_ids)
    base = leaderboard.summarize(eligible, costs=costs)
    facets = _facets(session, eligible_ids, eligible)
    distributions = _cost_distributions(session, eligible_ids)
    flips = _task_flips(session, eligible_ids)

    output = []
    for row in base:
        flip_count, flip_rate = flips.get(row.agent_config_id, (0, None))
        denominator = row.run_count * row.tasks_per_run
        output.append(
            AgentSummary(
                agent_config_id=row.agent_config_id,
                label=row.label,
                agent_name=row.agent_name,
                model_name=row.model_name,
                protocol_version=row.protocol_version,
                run_ids=list(row.run_ids),
                run_count=row.run_count,
                tasks_per_run=row.tasks_per_run,
                resolve_rate_mean=row.resolve_rate_mean,
                resolve_rate_min=row.resolve_rate_min,
                resolve_rate_max=row.resolve_rate_max,
                resolve_rate_spread=row.resolve_rate_spread,
                effective_resolve_rate_mean=row.effective_resolve_rate_mean,
                task_outcome_flip_count=flip_count,
                task_outcome_flip_rate=flip_rate,
                cost_usd_total=row.cost_usd_total,
                cost_per_task=row.cost_per_task,
                cost_reported_attempts=row.cost_reported_attempts,
                cost_estimated_attempts=row.cost_estimated_attempts,
                cost_unavailable_attempts=row.cost_unavailable_attempts,
                cost_distribution=distributions.get(
                    row.agent_config_id, CostDistribution(known_tasks=0, unavailable_tasks=0)
                ),
                total_tokens=row.total_tokens,
                tokens_per_task=row.tokens_per_task,
                makespan_ms_mean=row.makespan_ms_mean,
                infra_failure_total=row.infra_failure_total,
                infra_failure_rate=_rate(row.infra_failure_total, denominator),
                retry_total=row.retry_total,
                facets=facets.get((row.agent_config_id, row.protocol_version), {}),
            )
        )
    return output


def _host_metrics(
    path: Path | None,
) -> tuple[Availability, float | None, Availability, float | None]:
    if path is None:
        reason = "未提供宿主资源采样 CSV"
        return (
            Availability(available=False, reason=reason),
            None,
            Availability(available=False, reason=reason),
            None,
        )
    if not path.is_file():
        reason = f"找不到宿主资源采样文件：{path}"
        return (
            Availability(available=False, reason=reason),
            None,
            Availability(available=False, reason=reason),
            None,
        )
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    cpu = [float(row["cpu_pct"]) for row in rows if row.get("cpu_pct")]
    memory = [float(row["used_pct"]) for row in rows if row.get("used_pct")]
    cpu_availability = Availability(
        available=bool(cpu), reason=None if cpu else "采样文件没有 cpu_pct 列或有效值"
    )
    memory_availability = Availability(
        available=bool(memory), reason=None if memory else "采样文件没有 used_pct 列或有效值"
    )
    return (
        cpu_availability,
        max(cpu) if cpu else None,
        memory_availability,
        max(memory) if memory else None,
    )


def _performance(
    session: Session,
    run_ids: Sequence[int],
    runs: Sequence[RunSummary],
    *,
    host_metrics_csv: Path | None,
) -> PerformanceSummary:
    attempts = timing.load_attempts(session, run_ids)
    timing_rows = timing.summarize(attempts)
    timings = [
        TimingSummary(
            agent_name=item.agent_name,
            attempts=item.attempts,
            timeouts=item.timeouts,
            timeout_rate=item.timeout_rate,
            stages=[
                StageMetric(
                    stage=stage.stage,
                    samples=stage.samples,
                    mean_s=stage.mean_s,
                    p50_s=stage.p50_s,
                    p95_s=stage.p95_s,
                    max_s=stage.max_s,
                )
                for stage_name in timing.STAGES
                if (stage := item.stats.get(stage_name)) is not None
            ],
        )
        for item in timing_rows
    ]

    points = concurrency_mod.series_for(session, run_ids)
    concurrency = [
        ConcurrencyMetric(
            curve=curve,
            peak=(summary := concurrency_mod.summarize(points, curve)).peak,
            p50=summary.p50,
            span_s=summary.span_s,
        )
        for curve in concurrency_mod.CURVES
    ]
    concurrency_points = [
        ConcurrencyPoint(
            at=point.at,
            in_flight=point.in_flight,
            agent=point.agent,
            sandbox=point.sandbox,
        )
        for point in points
    ]

    wait_ms = sum(run.external_wait_ms for run in runs)
    makespan_ms = sum(run.makespan_ms or 0 for run in runs)
    if wait_ms > 0:
        external = Availability(available=True)
        wait_ratio = wait_ms / makespan_ms if makespan_ms else None
    else:
        external = Availability(
            available=False,
            reason="当前适配器没有写入 external_wait_ms；数据库默认 0 不能当成实测 0%",
        )
        wait_ratio = None

    cpu_status, cpu_peak, memory_status, memory_peak = _host_metrics(host_metrics_csv)
    actual = timing.actual_makespan_minutes(attempts)
    real = [item for item in timing_rows if item.agent_minutes > 0]
    projection_summary = None
    if real:
        inputs = MakespanInputs(
            runs=300,
            agent_minutes=max(item.agent_minutes for item in real),
            other_minutes=max(item.other_minutes for item in real),
            agent_limit=12,
            sandbox_limit=8,
            worker_slots=12,
            overhead_ratio=0.201,
        )
        projection = project(inputs)
        projection_summary = ProjectionSummary(
            target_runs=inputs.runs,
            target_machine="16 vCPU / 32 GiB",
            agent_limit=inputs.agent_limit,
            sandbox_limit=inputs.sandbox_limit,
            worker_slots=inputs.worker_slots or inputs.agent_limit,
            overhead_ratio=inputs.overhead_ratio,
            projected_hours=projection.projected_hours,
            bottleneck=projection.bottleneck,
            fits_six_hours=projection.fits(),
        )

    return PerformanceSummary(
        batch_makespan_minutes=actual,
        timings=timings,
        concurrency=concurrency,
        concurrency_points=concurrency_points,
        external_wait=external,
        external_wait_ratio=wait_ratio,
        host_cpu=cpu_status,
        host_cpu_peak_pct=cpu_peak,
        host_memory=memory_status,
        host_memory_peak_pct=memory_peak,
        projection=projection_summary,
    )


def _review_label(review: HumanReview, automatic: str) -> str | None:
    if review.action is HumanReviewAction.ACCEPT:
        return automatic
    if review.action is HumanReviewAction.MARK_TASK_DEFECT:
        return "N2_TASK_DEFECT"
    if review.action is HumanReviewAction.CORRECT and review.corrected_category is not None:
        return review.corrected_category.value
    return None


def _review_metrics(
    session: Session, run_ids: Sequence[int]
) -> tuple[Availability, float | None, Availability, float | None]:
    rows = session.execute(
        sa.select(HumanReview, FailureAttribution.category)
        .join(
            FailureAttribution,
            FailureAttribution.evaluation_task_run_id == HumanReview.evaluation_task_run_id,
        )
        .join(
            EvaluationTaskRun,
            EvaluationTaskRun.id == HumanReview.evaluation_task_run_id,
        )
        .where(EvaluationTaskRun.evaluation_run_id.in_(list(run_ids)))
        .order_by(HumanReview.evaluation_task_run_id, HumanReview.reviewed_at, HumanReview.id)
    ).all()
    labelled: list[tuple[int, str, str]] = []
    correct = 0
    for review, automatic_category in rows:
        automatic = automatic_category.value
        label = _review_label(review, automatic)
        if label is None:
            continue
        labelled.append((review.evaluation_task_run_id, review.reviewer, label))
        correct += int(label == automatic)
    if not labelled:
        reason = "没有可用于统计的人工盲检记录（E6-T3/E6-T4 未完成）"
        return (
            Availability(available=False, reason=reason),
            None,
            Availability(available=False, reason=reason),
            None,
        )

    accuracy = correct / len(labelled)
    by_task: dict[int, list[str]] = defaultdict(list)
    for task_run_id, _reviewer, label in labelled:
        by_task[task_run_id].append(label)
    pairs = [(labels[0], labels[1]) for labels in by_task.values() if len(labels) >= 2]
    if not pairs:
        return (
            Availability(available=True),
            accuracy,
            Availability(available=False, reason="没有同一案例的双人标注，无法计算 κ"),
            None,
        )
    observed = sum(1 for left, right in pairs if left == right) / len(pairs)
    left_counts = Counter(left for left, _ in pairs)
    right_counts = Counter(right for _, right in pairs)
    labels = set(left_counts) | set(right_counts)
    expected = sum(
        left_counts[label] / len(pairs) * right_counts[label] / len(pairs) for label in labels
    )
    kappa = 1.0 if expected == 1.0 and observed == 1.0 else (observed - expected) / (1 - expected)
    return Availability(available=True), accuracy, Availability(available=True), kappa


def _evidence_links(
    session: Session, task_run_ids: Sequence[int], *, base_url: str
) -> dict[int, tuple[str | None, str | None, str | None]]:
    """批量生成补丁、日志、轨迹链接，避免逐题查询。"""
    if not task_run_ids:
        return {}
    patch_ids = set(
        session.execute(
            sa.select(PatchArtifact.evaluation_task_run_id).where(
                PatchArtifact.evaluation_task_run_id.in_(list(task_run_ids)),
                PatchArtifact.kind == PatchKind.AGENT_NORMALIZED,
            )
        ).scalars()
    )
    artifact_rows = session.execute(
        sa.select(Artifact.owner_id, Artifact.kind).where(
            Artifact.owner_type == ArtifactOwnerType.TASK_RUN,
            Artifact.owner_id.in_(list(task_run_ids)),
            Artifact.kind.in_([ArtifactKind.AGENT_STDOUT, ArtifactKind.TRAJECTORY]),
        )
    ).all()
    artifact_kinds: dict[int, set[ArtifactKind]] = defaultdict(set)
    for owner_id, kind in artifact_rows:
        artifact_kinds[int(owner_id)].add(kind)

    base = base_url.rstrip("/")
    result = {}
    for task_run_id in task_run_ids:
        prefix = f"{base}/api/task-runs/{task_run_id}/artifacts"
        kinds = artifact_kinds[task_run_id]
        result[task_run_id] = (
            (f"{prefix}/{PatchKind.AGENT_NORMALIZED.value}" if task_run_id in patch_ids else None),
            (
                f"{prefix}/{ArtifactKind.AGENT_STDOUT.value}"
                if ArtifactKind.AGENT_STDOUT in kinds
                else None
            ),
            (
                f"{prefix}/{ArtifactKind.TRAJECTORY.value}"
                if ArtifactKind.TRAJECTORY in kinds
                else None
            ),
        )
    return result


def _task_results(session: Session, run_ids: Sequence[int], *, base_url: str) -> list[TaskResult]:
    """列出每一条 canonical 结果；报告可从汇总一路钻取到原始制品。"""
    rows = session.execute(
        sa.select(
            EvaluationTaskRun,
            EvaluationRun.id,
            AgentConfig.label,
            BenchmarkTask.task_id,
            BenchmarkTask.issue_title,
        )
        .join(EvaluationRun, EvaluationRun.id == EvaluationTaskRun.evaluation_run_id)
        .join(AgentConfig, AgentConfig.id == EvaluationRun.agent_config_id)
        .join(BenchmarkTask, BenchmarkTask.id == EvaluationTaskRun.benchmark_task_id)
        .where(
            EvaluationTaskRun.evaluation_run_id.in_(list(run_ids)),
            EvaluationTaskRun.is_canonical.is_(True),
        )
        .order_by(EvaluationRun.id, BenchmarkTask.task_id, EvaluationTaskRun.id)
    ).all()
    links = _evidence_links(session, [row[0].id for row in rows], base_url=base_url)
    output = []
    for task_run, run_id, label, task_id, issue_title in rows:
        patch_url, log_url, trajectory_url = links[task_run.id]
        output.append(
            TaskResult(
                task_run_id=task_run.id,
                run_id=int(run_id),
                agent_label=label,
                task_id=task_id,
                issue_title=issue_title,
                lifecycle_status=task_run.lifecycle_status.value,
                infra_outcome=(
                    task_run.infra_outcome.value if task_run.infra_outcome is not None else None
                ),
                agent_outcome=(
                    task_run.agent_outcome.value if task_run.agent_outcome is not None else None
                ),
                cost_source=(
                    task_run.cost_source.value if task_run.cost_source is not None else None
                ),
                cost_usd=task_run.cost_usd,
                patch_url=patch_url,
                log_url=log_url,
                trajectory_url=trajectory_url,
            )
        )
    return output


def _failures(
    session: Session, run_ids: Sequence[int], *, base_url: str, top_n: int
) -> FailureSummary:
    rows = session.execute(
        sa.select(
            EvaluationTaskRun,
            EvaluationRun.id,
            AgentConfig.label,
            BenchmarkTask.task_id,
            BenchmarkTask.issue_title,
            FailureAttribution,
        )
        .join(EvaluationRun, EvaluationRun.id == EvaluationTaskRun.evaluation_run_id)
        .join(AgentConfig, AgentConfig.id == EvaluationRun.agent_config_id)
        .join(BenchmarkTask, BenchmarkTask.id == EvaluationTaskRun.benchmark_task_id)
        .outerjoin(
            FailureAttribution,
            FailureAttribution.evaluation_task_run_id == EvaluationTaskRun.id,
        )
        .where(
            EvaluationTaskRun.evaluation_run_id.in_(list(run_ids)),
            EvaluationTaskRun.is_canonical.is_(True),
            EvaluationTaskRun.agent_outcome.is_distinct_from(AgentOutcome.RESOLVED),
        )
        .order_by(EvaluationTaskRun.id)
    ).all()
    task_run_ids = [row[0].id for row in rows]
    links = _evidence_links(session, task_run_ids, base_url=base_url)

    categories: Counter[str] = Counter()
    heatmap: Counter[tuple[str, str]] = Counter()
    attributed = 0
    llm_count = 0
    cases: list[FailureCase] = []
    for task_run, run_id, label, task_id, issue_title, attribution in rows:
        category = attribution.category.value if attribution is not None else None
        if category is not None:
            attributed += 1
            categories[category] += 1
            heatmap[(label, category)] += 1
        if attribution is not None and attribution.stage is AttributionStage.LLM:
            llm_count += 1
        if len(cases) < top_n:
            patch_url, log_url, trajectory_url = links[task_run.id]
            cases.append(
                FailureCase(
                    task_run_id=task_run.id,
                    run_id=int(run_id),
                    agent_label=label,
                    task_id=task_id,
                    issue_title=issue_title,
                    infra_outcome=(
                        task_run.infra_outcome.value if task_run.infra_outcome is not None else None
                    ),
                    agent_outcome=(
                        task_run.agent_outcome.value if task_run.agent_outcome is not None else None
                    ),
                    category=category,
                    attribution_status=(
                        attribution.status.value if attribution is not None else None
                    ),
                    reasoning_zh=(attribution.reasoning_zh if attribution is not None else None),
                    patch_url=patch_url,
                    log_url=log_url,
                    trajectory_url=trajectory_url,
                )
            )
    accuracy_state, accuracy, kappa_state, kappa = _review_metrics(session, run_ids)
    llm_state = Availability(
        available=llm_count > 0,
        reason=None if llm_count else "没有 LLM 阶段归因（E6-T2 未完成或所选运行未执行）",
    )
    return FailureSummary(
        total_failures=len(rows),
        attributed_failures=attributed,
        unattributed_failures=len(rows) - attributed,
        category_counts=dict(sorted(categories.items())),
        heatmap=[
            FailureCell(agent_label=label, category=category, count=count)
            for (label, category), count in sorted(heatmap.items())
        ],
        top_cases=cases,
        llm_attribution=llm_state,
        review_accuracy=accuracy_state,
        review_accuracy_value=accuracy,
        kappa=kappa_state,
        kappa_value=kappa,
    )


def build_report(
    session: Session,
    run_ids: Sequence[int],
    *,
    title: str = "AI Coding Agent 评测对比报告",
    base_url: str = "http://localhost:8000",
    top_n: int = 10,
    host_metrics_csv: Path | None = None,
    generated_at: datetime | None = None,
) -> ReportData:
    """生成一份报告的全部事实；不写文件、不提交事务。"""
    rows = _load_run_rows(session, run_ids)
    dataset = _validate_comparability(rows)
    ordered_ids = [run.id for run, _, _, _ in rows]
    runs = _run_summaries(rows)
    agents = _agent_summaries(session, dataset.id, ordered_ids)
    task_results = _task_results(session, ordered_ids, base_url=base_url)
    failures = _failures(session, ordered_ids, base_url=base_url, top_n=top_n)
    performance = _performance(session, ordered_ids, runs, host_metrics_csv=host_metrics_csv)

    warnings: list[str] = []
    real_configs = {config.id for _, config, agent, _ in rows if agent.kind not in _SENTINELS}
    if len(real_configs) < 3:
        warnings.append(
            f"DEL-03 未达标：所选运行只有 {len(real_configs)} 个真实 Agent 配置，"
            "要求至少 3 个；哨兵不计入。"
        )
    selected_eligible = {run_id for agent in agents for run_id in agent.run_ids}
    excluded = [
        run.id
        for run, _, agent, _ in rows
        if agent.kind not in _SENTINELS and run.id not in selected_eligible
    ]
    if excluded:
        warnings.append(
            "以下真实 Agent 运行不符合排行榜六条准入规则，未进入对比指标："
            + ", ".join(f"#{run_id}" for run_id in excluded)
        )
    if not failures.llm_attribution.available:
        warnings.append(failures.llm_attribution.reason or "LLM 归因不可用")
    if not failures.review_accuracy.available:
        warnings.append(failures.review_accuracy.reason or "抽检准确率不可用")
    if not performance.external_wait.available:
        warnings.append(performance.external_wait.reason or "external_wait 不可用")
    if not performance.host_cpu.available:
        warnings.append(performance.host_cpu.reason or "CPU 峰值不可用")

    return ReportData(
        generated_at=generated_at or datetime.now(tz=UTC),
        title=title,
        scope=(ReportScope.SINGLE_RUN if len(ordered_ids) == 1 else ReportScope.COMPARISON).value,
        run_ids=ordered_ids,
        dataset=DatasetInfo(
            id=dataset.id,
            slug=dataset.slug,
            version=dataset.version,
            title=dataset.title,
            task_count=dataset.task_count,
            snapshot_digest=dataset.snapshot_digest,
        ),
        warnings=warnings,
        runs=runs,
        agents=agents,
        task_results=task_results,
        performance=performance,
        failures=failures,
    )


__all__ = ["ReportInputError", "build_report"]
