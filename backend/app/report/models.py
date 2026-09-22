"""报告生成器的稳定中间结构。

查询数据库、计算指标、渲染三种格式彼此分开。三种渲染器只认这里的
``ReportData``，这样 HTML、Markdown、JSON 不会各算一遍指标后慢慢漂移。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ReportModel(BaseModel):
    """报告结构的公共基类；冻结后渲染器不能偷偷改聚合结果。"""

    model_config = ConfigDict(frozen=True)


class Availability(ReportModel):
    """一个指标是否真的采到了，避免把缺数据展示成 0。"""

    available: bool
    reason: str | None = None


class DatasetInfo(ReportModel):
    """本报告涉及的一版数据集快照。"""

    id: int
    slug: str
    version: str
    title: str
    task_count: int
    snapshot_digest: str | None


class RunSummary(ReportModel):
    """一轮实验的原始汇总，便于复核均值来自哪些运行。"""

    id: int
    name: str
    agent_config_id: int
    agent_label: str
    agent_name: str
    agent_kind: str
    protocol_version: str
    status: str
    total_tasks: int
    completed_tasks: int
    resolved_count: int
    strict_resolve_rate: Decimal | None
    effective_resolve_rate: Decimal | None
    infra_failure_count: int
    retry_count: int
    recovered_infra_failure_count: int
    total_cost_usd: Decimal
    total_tokens: int
    makespan_ms: int | None
    external_wait_ms: int
    dirty: bool
    excluded_reason: str | None
    sigkill_without_oom_flag_count: int = 0
    dataset_label: str = ""


class FacetMetric(ReportModel):
    """难度、语言或仓库分面中的一个格子。"""

    value: str
    resolved: int
    total: int
    resolve_rate: Decimal | None


class CostDistribution(ReportModel):
    """按运行×题目累计重试 attempt 后的费用分布。"""

    known_tasks: int
    unavailable_tasks: int
    p50_usd: Decimal | None = None
    p95_usd: Decimal | None = None


class AgentSummary(ReportModel):
    """同一个 Agent 配置在多轮实验中的对比指标。"""

    agent_config_id: int
    label: str
    agent_name: str
    model_name: str
    protocol_version: str
    run_ids: list[int]
    run_count: int
    tasks_per_run: int
    resolve_rate_mean: Decimal | None
    resolve_rate_min: Decimal | None
    resolve_rate_max: Decimal | None
    resolve_rate_spread: Decimal | None
    effective_resolve_rate_mean: Decimal | None
    task_outcome_flip_count: int
    task_outcome_flip_rate: Decimal | None
    cost_usd_total: Decimal
    cost_per_task: Decimal | None
    #: `cost_per_task` 是下界（有 attempt 报不出成本）。渲染时数字前加 "≥"。
    cost_lower_bound: bool
    cost_reported_attempts: int
    cost_estimated_attempts: int
    cost_unavailable_attempts: int
    cost_distribution: CostDistribution
    total_tokens: int
    tokens_per_task: int | None
    makespan_ms_mean: int | None
    infra_failure_total: int
    infra_failure_rate: Decimal | None
    retry_total: int
    facets: dict[str, list[FacetMetric]]
    dataset_label: str = ""
    patch_different_flip_count: int = 0
    patch_different_flip_rate: Decimal | None = None
    same_nonempty_patch_flip_count: int = 0
    same_nonempty_patch_flip_rate: Decimal | None = None
    same_empty_patch_status_flip_count: int = 0
    missing_patch_flip_count: int = 0
    no_agent_conclusion_flip_count: int = 0
    patch_consistency_alarm: bool = False
    sigkill_without_oom_flag_count: int = 0


class CombinedSourceResult(ReportModel):
    """合并表中一个 Agent 在一版题集上的各轮结果。"""

    dataset_label: str
    task_count: int
    run_ids: list[int]
    resolved_counts: list[int]


class CombinedAgentRow(ReportModel):
    """合并表的一行；各来源各算各的解决数，不合算解决率。"""

    agent_config_id: int
    label: str
    model_name: str
    sources: list[CombinedSourceResult]


class StageMetric(ReportModel):
    """一个 Agent 在一个阶段上的耗时分布，单位秒。"""

    stage: str
    samples: int
    mean_s: float
    p50_s: float
    p95_s: float
    max_s: float


class TimingSummary(ReportModel):
    """一个 Agent 的阶段耗时与超时率。"""

    agent_name: str
    attempts: int
    timeouts: int
    timeout_rate: float
    stages: list[StageMetric]


class ConcurrencyMetric(ReportModel):
    """有效并发曲线的摘要。"""

    curve: str
    peak: int
    p50: int
    span_s: float


class ConcurrencyPoint(ReportModel):
    """有效并发阶梯图的一个变化点。"""

    at: datetime
    in_flight: int
    agent: int
    sandbox: int


class ProjectionSummary(ReportModel):
    """把实测 A/S 外推到目标机器后的 makespan。"""

    target_runs: int
    target_machine: str
    agent_limit: int
    sandbox_limit: int
    worker_slots: int
    overhead_ratio: float
    projected_hours: float
    bottleneck: str
    fits_six_hours: bool


class PerformanceSummary(ReportModel):
    """DEL-05 所需的性能数据和缺失声明。"""

    batch_makespan_minutes: float | None
    timings: list[TimingSummary]
    concurrency: list[ConcurrencyMetric]
    concurrency_points: list[ConcurrencyPoint]
    external_wait: Availability
    external_wait_ratio: float | None
    host_cpu: Availability
    host_cpu_peak_pct: float | None
    host_memory: Availability
    host_memory_peak_pct: float | None
    projection: ProjectionSummary | None


class FailureCell(ReportModel):
    """Agent × 失败类别热力图中的一个格子。"""

    agent_label: str
    category: str
    count: int


class FailureCase(ReportModel):
    """Top-N 失败案例及其证据链接。"""

    task_run_id: int
    run_id: int
    agent_label: str
    task_id: str
    issue_title: str
    infra_outcome: str | None
    agent_outcome: str | None
    category: str | None
    attribution_status: str | None
    reasoning_zh: str | None
    patch_url: str | None
    log_url: str | None
    trajectory_url: str | None
    dataset_label: str = ""


class TaskResult(ReportModel):
    """一条 canonical 题目结果及其可复核制品链接。"""

    task_run_id: int
    run_id: int
    agent_label: str
    task_id: str
    issue_title: str
    lifecycle_status: str
    infra_outcome: str | None
    agent_outcome: str | None
    cost_source: str | None
    cost_usd: Decimal | None
    patch_url: str | None
    log_url: str | None
    trajectory_url: str | None
    dataset_label: str = ""


class FailureSummary(ReportModel):
    """DEL-04 的现有数据；没有完成的归因和盲检必须显式缺席。"""

    total_failures: int
    attributed_failures: int
    unattributed_failures: int
    #: 自动归因给出了类别但自己标了 NEEDS_HUMAN（可信度不够，要进人工队列）的条数。
    #: 它们**包含在** attributed_failures 和 category_counts 里，页面上要单独标出来。
    needs_human_failures: int = 0
    #: 由 LLM 层给出结论的条数（其余是规则层 / 人工）。
    llm_attributed_failures: int = 0
    category_counts: dict[str, int]
    heatmap: list[FailureCell]
    top_cases: list[FailureCase]
    llm_attribution: Availability
    review_accuracy: Availability
    review_accuracy_value: float | None
    kappa: Availability
    kappa_value: float | None


class ReportData(ReportModel):
    """HTML、Markdown、JSON 共用的唯一报告数据。"""

    schema_version: str = "2.0"
    generated_at: datetime
    title: str
    scope: str
    run_ids: list[int]
    dataset: DatasetInfo | None
    warnings: list[str]
    runs: list[RunSummary]
    agents: list[AgentSummary]
    task_results: list[TaskResult]
    performance: PerformanceSummary
    failures: FailureSummary
    datasets: list[DatasetInfo] = []
    combined_task_count: int | None = None
    combined_agents: list[CombinedAgentRow] = []
    sigkill_without_oom_flag_count: int = 0


__all__ = [
    "AgentSummary",
    "Availability",
    "CombinedAgentRow",
    "CombinedSourceResult",
    "ConcurrencyMetric",
    "ConcurrencyPoint",
    "CostDistribution",
    "DatasetInfo",
    "FacetMetric",
    "FailureCase",
    "FailureCell",
    "FailureSummary",
    "PerformanceSummary",
    "ProjectionSummary",
    "ReportData",
    "RunSummary",
    "StageMetric",
    "TaskResult",
    "TimingSummary",
]
