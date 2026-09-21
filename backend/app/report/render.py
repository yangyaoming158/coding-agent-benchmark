"""把统一的报告数据渲染成 HTML、Markdown 和 JSON。"""

# HTML/CSS 模板和 Markdown 表格标题保持连续更容易审查。
# ruff: noqa: E501

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from html import escape
from typing import Final

from app.domain.enums import FailureCategory
from app.report.models import (
    AgentSummary,
    Availability,
    DatasetInfo,
    FailureCase,
    ReportData,
    TaskResult,
)

_MISSING: Final = "不可用"


def _pct(value: Decimal | float | None) -> str:
    if value is None:
        return _MISSING
    return f"{float(value):.1%}"


def _money(value: Decimal | None) -> str:
    if value is None:
        return _MISSING
    return f"${value:.4f}"


def _per_task_cost(agent: AgentSummary) -> str:
    """每题成本；是下界时前面加 "≥"，读的人一眼知道真实数只会更高。"""
    text = _money(agent.cost_per_task)
    if agent.cost_per_task is not None and agent.cost_lower_bound:
        return f"≥ {text}"
    return text


def _seconds(value: float | None) -> str:
    return _MISSING if value is None else f"{value:.2f}s"


def _availability(value: Availability, metric: str | None = None) -> str:
    if value.available:
        return metric or "已采集"
    return f"{_MISSING}（{value.reason or '没有数据'}）"


def _md_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(_md_cell(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def render_json(report: ReportData) -> str:
    """输出稳定、可机器读取的 JSON；缺失值保持为 ``null``。"""
    return (
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )


def _cost_note(agent: AgentSummary) -> str:
    counts = (
        f"reported {agent.cost_reported_attempts} / "
        f"estimated {agent.cost_estimated_attempts} / "
        f"unavailable {agent.cost_unavailable_attempts}"
    )
    if agent.cost_unavailable_attempts:
        return f"{counts}；有缺失，费用合计不是完整总成本"
    return counts


def _case_links(case: FailureCase | TaskResult) -> str:
    links = []
    for label, url in (
        ("补丁", case.patch_url),
        ("日志", case.log_url),
        ("轨迹", case.trajectory_url),
    ):
        if url:
            links.append(f"[{label}]({url})")
    return " / ".join(links) or _MISSING


def _datasets(report: ReportData) -> list[DatasetInfo]:
    return report.datasets or ([report.dataset] if report.dataset is not None else [])


def _combined_headers(report: ReportData) -> list[str]:
    return ["Agent", *[f"{item.slug}@{item.version}" for item in _datasets(report)], "总题数"]


def _combined_cells(report: ReportData) -> list[list[object]]:
    """各版逐轮显示解决数；总题数只说明覆盖范围，不生成混合解决率。"""
    return [
        [
            agent.label,
            *[
                "；".join(
                    f"#{run_id} {count}/{source.task_count}"
                    for run_id, count in zip(source.run_ids, source.resolved_counts, strict=True)
                )
                or _MISSING
                for source in agent.sources
            ],
            report.combined_task_count or _MISSING,
        ]
        for agent in report.combined_agents
    ]


def render_markdown(report: ReportData) -> str:
    """渲染便于代码评审和归档的 Markdown 报告。"""
    lines = [
        f"# {report.title}",
        "",
        f"生成时间：{report.generated_at.isoformat()}  ",
        f"范围：{report.scope}；运行：{', '.join('#' + str(i) for i in report.run_ids)}  ",
        "数据集："
        + "、".join(
            f"{item.slug}@{item.version}（{item.task_count} 题）" for item in _datasets(report)
        )
        + "  ",
        "快照："
        + "、".join(
            f"{item.slug}@{item.version}={item.snapshot_digest or _MISSING}"
            for item in _datasets(report)
        ),
        "",
        "## 数据完整性说明",
        "",
    ]
    lines.extend(f"- {warning}" for warning in report.warnings)
    if not report.warnings:
        lines.append("- 未发现缺失或范围警告。")

    if report.combined_agents:
        lines += [
            "",
            "## 跨数据集合并表",
            "",
            "每个来源分别统计；总题数为各版题数之和，不计算混合解决率。",
            "",
            _md_table(_combined_headers(report), _combined_cells(report)),
        ]

    agent_rows = [
        [
            agent.label,
            agent.dataset_label
            or (f"{report.dataset.slug}@{report.dataset.version}" if report.dataset else _MISSING),
            agent.model_name,
            agent.run_count,
            _pct(agent.resolve_rate_mean),
            _pct(agent.effective_resolve_rate_mean),
            _pct(agent.resolve_rate_min),
            _pct(agent.resolve_rate_max),
            _pct(agent.resolve_rate_spread),
            _pct(agent.task_outcome_flip_rate),
            f"{agent.patch_different_flip_count}（{_pct(agent.patch_different_flip_rate)}）",
            f"{agent.same_nonempty_patch_flip_count}（{_pct(agent.same_nonempty_patch_flip_rate)}）"
            + (" ⚠ 平台报警" if agent.patch_consistency_alarm else ""),
            agent.infra_failure_total,
            agent.sigkill_without_oom_flag_count,
            agent.retry_total,
        ]
        for agent in report.agents
    ]
    lines += [
        "",
        "## Agent 对比",
        "",
        _md_table(
            [
                "Agent",
                "数据集",
                "模型",
                "轮数",
                "严格解决率",
                "有效解决率",
                "最低",
                "最高",
                "轮间极差",
                "逐题翻转率",
                "补丁不同",
                "同补丁不同结论",
                "平台故障",
                "137 无 OOM 标志（推算）",
                "重试",
            ],
            agent_rows,
        ),
        "",
        "注：同补丁只统计同一份非空标准化补丁；空补丁失败状态变化单独记录，不触发平台报警。",
        "空补丁状态变化："
        + "、".join(
            f"{agent.label} × {agent.dataset_label}: {agent.same_empty_patch_status_flip_count}"
            for agent in report.agents
        )
        + "。",
        "无 Agent 结论的翻转："
        + "、".join(
            f"{agent.label} × {agent.dataset_label}: {agent.no_agent_conclusion_flip_count}"
            for agent in report.agents
        )
        + "；保留在总翻转率中，不纳入补丁对比。",
        "",
        "### 137 无 OOM 标志次数（推算）",
        "",
        "按所有 attempt 统计：exit_code=137，且 infra_outcome 不属于 OOM_KILLED / AGENT_TIMEOUT / TEST_TIMEOUT；这是数据库推算值，不是 Worker 日志条数。",
        f"报告合计：{report.sigkill_without_oom_flag_count} 次。",
        "",
        _md_table(
            ["运行", "Agent", "数据集", "137 无 OOM 标志（推算）"],
            [
                [
                    f"#{run.id}",
                    run.agent_label,
                    run.dataset_label,
                    run.sigkill_without_oom_flag_count,
                ]
                for run in report.runs
            ],
        ),
        "",
        "## 成本—解决率",
        "",
        _md_table(
            ["Agent", "数据集", "严格解决率", "每题成本", "P50", "P95", "费用来源"],
            [
                [
                    agent.label,
                    agent.dataset_label,
                    _pct(agent.resolve_rate_mean),
                    _per_task_cost(agent),
                    _money(agent.cost_distribution.p50_usd),
                    _money(agent.cost_distribution.p95_usd),
                    _cost_note(agent),
                ]
                for agent in report.agents
            ],
        ),
    ]

    for facet, title in (("difficulty", "难度"), ("language", "题面语言"), ("repository", "仓库")):
        rows: list[list[object]] = []
        for agent in report.agents:
            rows.extend(
                [
                    agent.label,
                    agent.dataset_label,
                    cell.value,
                    f"{cell.resolved}/{cell.total}",
                    _pct(cell.resolve_rate),
                ]
                for cell in agent.facets.get(facet, [])
            )
        lines += [
            "",
            f"### 按{title}",
            "",
            _md_table(["Agent", "数据集", title, "解决数", "解决率"], rows),
        ]

    lines += [
        "",
        "## 每题结果与制品",
        "",
        _md_table(
            [
                "运行",
                "Agent",
                "数据集",
                "题目",
                "平台结果",
                "Agent 结果",
                "成本来源",
                "成本",
                "制品",
            ],
            [
                [
                    f"#{row.run_id}",
                    row.agent_label,
                    row.dataset_label or _MISSING,
                    f"{row.task_id} — {row.issue_title}",
                    row.infra_outcome or _MISSING,
                    row.agent_outcome or _MISSING,
                    row.cost_source or _MISSING,
                    _money(row.cost_usd) if row.cost_source != "unavailable" else _MISSING,
                    _case_links(row),
                ]
                for row in report.task_results
            ],
        ),
    ]

    performance = report.performance
    lines += [
        "",
        "## 性能与容量",
        "",
        f"- 批次 makespan：{_seconds(performance.batch_makespan_minutes * 60 if performance.batch_makespan_minutes is not None else None)}",
        f"- 外部等待占比：{_availability(performance.external_wait, _pct(performance.external_wait_ratio))}",
        f"- 宿主 CPU 峰值：{_availability(performance.host_cpu, _pct((performance.host_cpu_peak_pct or 0) / 100) if performance.host_cpu_peak_pct is not None else None)}",
        f"- 宿主内存峰值：{_availability(performance.host_memory, _pct((performance.host_memory_peak_pct or 0) / 100) if performance.host_memory_peak_pct is not None else None)}",
        "",
        _md_table(
            ["Agent", "阶段", "样本", "均值", "P50", "P95", "最大"],
            [
                [
                    timing.agent_name,
                    stage.stage,
                    stage.samples,
                    _seconds(stage.mean_s),
                    _seconds(stage.p50_s),
                    _seconds(stage.p95_s),
                    _seconds(stage.max_s),
                ]
                for timing in performance.timings
                for stage in timing.stages
            ],
        ),
        "",
        _md_table(
            ["并发曲线", "峰值", "时间加权 P50", "跨度"],
            [
                [row.curve, row.peak, row.p50, _seconds(row.span_s)]
                for row in performance.concurrency
            ],
        ),
    ]
    if performance.projection is not None:
        p = performance.projection
        lines += [
            "",
            f"容量外推：{p.target_machine}，{p.target_runs} 次运行，Agent/Sandbox/Worker "
            f"并发 {p.agent_limit}/{p.sandbox_limit}/{p.worker_slots}，预计 {p.projected_hours:.2f} 小时；"
            f"瓶颈为 {p.bottleneck}，6 小时目标{'满足' if p.fits_six_hours else '不满足'}。",
        ]

    failure = report.failures
    category_rows = []
    for category in FailureCategory:
        category_rows.append([category.value, failure.category_counts.get(category.value, 0)])
    category_rows.append(["UNATTRIBUTED", failure.unattributed_failures])
    lines += [
        "",
        "## 失败归因",
        "",
        f"失败 {failure.total_failures} 次；已归因 {failure.attributed_failures}；未归因 {failure.unattributed_failures}。",
        "",
        _md_table(["类别", "数量"], category_rows),
        "",
        "### Agent × 失败类别",
        "",
        _md_table(
            ["Agent", "类别", "数量"],
            [[cell.agent_label, cell.category, cell.count] for cell in failure.heatmap],
        ),
        "",
        f"- LLM 归因：{_availability(failure.llm_attribution)}",
        f"- 盲检准确率：{_availability(failure.review_accuracy, _pct(failure.review_accuracy_value))}",
        f"- Cohen's κ：{_availability(failure.kappa, f'{failure.kappa_value:.3f}' if failure.kappa_value is not None else None)}",
        "",
        "### Top 失败案例",
        "",
        _md_table(
            ["运行", "Agent", "数据集", "题目", "类别", "判定", "制品"],
            [
                [
                    f"#{case.run_id}",
                    case.agent_label,
                    case.dataset_label,
                    f"{case.task_id} — {case.issue_title}",
                    case.category or "UNATTRIBUTED",
                    case.agent_outcome or case.infra_outcome or _MISSING,
                    _case_links(case),
                ]
                for case in failure.top_cases
            ],
        ),
        "",
    ]
    return "\n".join(lines)


def _html_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"<div class=table-wrap><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def _scatter(report: ReportData) -> str:
    points = [
        a for a in report.agents if a.cost_per_task is not None and a.resolve_rate_mean is not None
    ]
    if not points:
        return '<p class="missing">没有同时具备费用与解决率的数据。</p>'
    max_cost = max(float(a.cost_per_task or 0) for a in points) or 1.0
    circles = []
    for index, agent in enumerate(points):
        x = 55 + 560 * float(agent.cost_per_task or 0) / max_cost
        y = 320 - 270 * float(agent.resolve_rate_mean or 0)
        color = ("#36c5b0", "#ffb454", "#8da2fb", "#f07178")[index % 4]
        circles.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="{color}"><title>'
            f"{escape(agent.label)} × {escape(agent.dataset_label)}：{escape(_per_task_cost(agent))} / {escape(_pct(agent.resolve_rate_mean))}"
            "</title></circle>"
            f'<text x="{x + 11:.1f}" y="{y + 4:.1f}">{escape(agent.label)} · {escape(agent.dataset_label)}</text>'
        )
    return (
        '<svg class="chart" viewBox="0 0 680 360" role="img" aria-label="成本解决率散点图">'
        '<line x1="55" y1="320" x2="635" y2="320"/><line x1="55" y1="40" x2="55" y2="320"/>'
        '<text x="290" y="352">每题成本（越左越好）</text><text x="8" y="30">解决率（越高越好）</text>'
        + "".join(circles)
        + "</svg>"
    )


def _html_links(case: FailureCase | TaskResult) -> str:
    links = []
    for label, url in (
        ("补丁", case.patch_url),
        ("日志", case.log_url),
        ("轨迹", case.trajectory_url),
    ):
        if url:
            links.append(f'<a href="{escape(url, quote=True)}">{label}</a>')
    return " / ".join(links) or _MISSING


def _task_result_table(rows: Sequence[TaskResult]) -> str:
    """逐题表的链接保留为 HTML，其余来自数据库的文本全部转义。"""
    body = "".join(
        "<tr>"
        f"<td>#{row.run_id}</td><td>{escape(row.agent_label)}</td>"
        f"<td>{escape(row.dataset_label or _MISSING)}</td>"
        f"<td>{escape(row.task_id)} — {escape(row.issue_title)}</td>"
        f"<td>{escape(row.infra_outcome or _MISSING)}</td>"
        f"<td>{escape(row.agent_outcome or _MISSING)}</td>"
        f"<td>{escape(row.cost_source or _MISSING)}</td>"
        f"<td>{escape(_money(row.cost_usd) if row.cost_source != 'unavailable' else _MISSING)}</td>"
        f"<td>{_html_links(row)}</td></tr>"
        for row in rows
    )
    return (
        '<div class="table-wrap"><table><thead><tr><th>运行</th><th>Agent</th><th>数据集</th>'
        "<th>题目</th><th>平台结果</th><th>Agent 结果</th><th>成本来源</th>"
        f"<th>成本</th><th>制品</th></tr></thead><tbody>{body}</tbody></table></div>"
    )


def render_html(report: ReportData) -> str:
    """渲染无外部依赖、可直接归档打开的 HTML 报告。"""
    warnings = (
        "".join(f"<li>{escape(item)}</li>" for item in report.warnings)
        or "<li>未发现缺失或范围警告。</li>"
    )
    agent_rows = [
        [
            a.label,
            a.dataset_label
            or (f"{report.dataset.slug}@{report.dataset.version}" if report.dataset else _MISSING),
            a.model_name,
            a.run_count,
            _pct(a.resolve_rate_mean),
            _pct(a.effective_resolve_rate_mean),
            _pct(a.resolve_rate_spread),
            _pct(a.task_outcome_flip_rate),
            f"{a.patch_different_flip_count}（{_pct(a.patch_different_flip_rate)}）",
            f"{a.same_nonempty_patch_flip_count}（{_pct(a.same_nonempty_patch_flip_rate)}）"
            + (" ⚠ 平台报警" if a.patch_consistency_alarm else ""),
            a.infra_failure_total,
            a.sigkill_without_oom_flag_count,
            a.retry_total,
        ]
        for a in report.agents
    ]
    cost_rows = [
        [
            a.label,
            a.dataset_label,
            _pct(a.resolve_rate_mean),
            _per_task_cost(a),
            _money(a.cost_distribution.p50_usd),
            _money(a.cost_distribution.p95_usd),
            _cost_note(a),
        ]
        for a in report.agents
    ]
    stage_rows = [
        [
            timing.agent_name,
            stage.stage,
            stage.samples,
            _seconds(stage.mean_s),
            _seconds(stage.p50_s),
            _seconds(stage.p95_s),
            _seconds(stage.max_s),
        ]
        for timing in report.performance.timings
        for stage in timing.stages
    ]
    categories = [
        [category.value, report.failures.category_counts.get(category.value, 0)]
        for category in FailureCategory
    ]
    categories.append(["UNATTRIBUTED", report.failures.unattributed_failures])
    top_rows = "".join(
        "<tr>"
        f"<td>#{case.run_id}</td><td>{escape(case.agent_label)}</td>"
        f"<td>{escape(case.dataset_label or _MISSING)}</td>"
        f"<td>{escape(case.task_id)} — {escape(case.issue_title)}</td>"
        f"<td>{escape(case.category or 'UNATTRIBUTED')}</td>"
        f"<td>{escape(case.agent_outcome or case.infra_outcome or _MISSING)}</td>"
        f"<td>{_html_links(case)}</td></tr>"
        for case in report.failures.top_cases
    )
    projection = "不可用"
    combined = ""
    if report.combined_agents:
        combined = (
            "<h2>跨数据集合并表</h2><p>每个来源分别统计；总题数为各版题数之和，不计算混合解决率。</p>"
            + _html_table(_combined_headers(report), _combined_cells(report))
        )
    dataset_meta = "、".join(
        f"{item.slug}@{item.version} · {item.task_count} 题" for item in _datasets(report)
    )
    empty_patch_note = "、".join(
        f"{agent.label} × {agent.dataset_label}: {agent.same_empty_patch_status_flip_count}"
        for agent in report.agents
    )
    no_conclusion_note = "、".join(
        f"{agent.label} × {agent.dataset_label}: {agent.no_agent_conclusion_flip_count}"
        for agent in report.agents
    )
    alarm_banner = (
        '<p class="alarm">平台报警：同一份非空标准化补丁在不同轮次得出不同结论，请核查判定证据。</p>'
        if any(agent.patch_consistency_alarm for agent in report.agents)
        else ""
    )
    if report.performance.projection is not None:
        p = report.performance.projection
        projection = (
            f"{escape(p.target_machine)}、{p.target_runs} 次运行：预计 {p.projected_hours:.2f} 小时；"
            f"瓶颈 {escape(p.bottleneck)}；6 小时目标{'满足' if p.fits_six_hours else '不满足'}。"
        )
    css = """
    :root{color-scheme:dark;--bg:#0b1020;--panel:#131b2f;--ink:#e8edf7;--muted:#9aa8c2;--line:#2a3754;--accent:#36c5b0}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,sans-serif}
    main{max-width:1180px;margin:auto;padding:38px 24px 80px}h1{font-size:34px;margin:0 0 8px}h2{margin-top:38px;border-bottom:1px solid var(--line);padding-bottom:8px}
    .meta,.missing{color:var(--muted)}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin:18px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
    .stat{background:#0e1629;border-radius:8px;padding:14px}.stat b{display:block;color:var(--accent);font-size:22px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;margin:12px 0 20px}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line);vertical-align:top}th{color:#b9c8e8;white-space:nowrap}a{color:#65d7c7}.alarm{color:#ff6b6b;font-weight:700}.chart{max-width:720px;width:100%;background:#0e1629;border-radius:10px}.chart line{stroke:#657595;stroke-width:1}.chart text{fill:#cfd8ea;font-size:12px}
    """
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(report.title)}</title><style>{css}</style></head><body><main>
<h1>{escape(report.title)}</h1>
<p class="meta">{escape(dataset_meta)} · 运行 {escape(", ".join("#" + str(i) for i in report.run_ids))} · {escape(report.generated_at.isoformat())}</p>
<section class="card"><h2>数据完整性说明</h2><ul>{warnings}</ul></section>
{combined}
<h2>Agent 对比</h2>{alarm_banner}{_html_table(["Agent", "数据集", "模型", "轮数", "严格解决率", "有效解决率", "轮间极差", "逐题翻转率", "补丁不同", "同补丁不同结论", "平台故障", "137 无 OOM 标志（推算）", "重试"], agent_rows)}
<p>同补丁只统计同一份非空标准化补丁；空补丁失败状态变化单独记录，不触发平台报警。空补丁状态变化：{escape(empty_patch_note)}。</p>
<p>无 Agent 结论的翻转：{escape(no_conclusion_note)}；保留在总翻转率中，不纳入补丁对比。</p>
<h3>137 无 OOM 标志次数（推算）</h3><p>按所有 attempt 统计：exit_code=137，且 infra_outcome 不属于 OOM_KILLED / AGENT_TIMEOUT / TEST_TIMEOUT；这是数据库推算值，不是 Worker 日志条数。报告合计：{report.sigkill_without_oom_flag_count} 次。</p>
{_html_table(["运行", "Agent", "数据集", "137 无 OOM 标志（推算）"], [[f"#{run.id}", run.agent_label, run.dataset_label, run.sigkill_without_oom_flag_count] for run in report.runs])}
<h2>成本—解决率</h2>{_scatter(report)}{_html_table(["Agent", "数据集", "严格解决率", "每题成本", "P50", "P95", "费用来源"], cost_rows)}
<h2>每题结果与制品</h2>{_task_result_table(report.task_results)}
<h2>性能与容量</h2><div class="grid">
<div class="stat"><span>批次 makespan</span><b>{escape(_seconds(report.performance.batch_makespan_minutes * 60 if report.performance.batch_makespan_minutes is not None else None))}</b></div>
<div class="stat"><span>外部等待</span><b>{escape(_availability(report.performance.external_wait, _pct(report.performance.external_wait_ratio)))}</b></div>
<div class="stat"><span>宿主 CPU 峰值</span><b>{escape(_availability(report.performance.host_cpu, f"{report.performance.host_cpu_peak_pct:.1f}%" if report.performance.host_cpu_peak_pct is not None else None))}</b></div>
<div class="stat"><span>宿主内存峰值</span><b>{escape(_availability(report.performance.host_memory, f"{report.performance.host_memory_peak_pct:.1f}%" if report.performance.host_memory_peak_pct is not None else None))}</b></div></div>
{_html_table(["Agent", "阶段", "样本", "均值", "P50", "P95", "最大"], stage_rows)}
{_html_table(["并发曲线", "峰值", "时间加权 P50", "跨度"], [[x.curve, x.peak, x.p50, _seconds(x.span_s)] for x in report.performance.concurrency])}
<p>{projection}</p>
<h2>失败归因</h2><p>失败 {report.failures.total_failures} 次；已归因 {report.failures.attributed_failures}；未归因 {report.failures.unattributed_failures}。</p>
{_html_table(["类别", "数量"], categories)}
<h3>Agent × 失败类别</h3>{_html_table(["Agent", "类别", "数量"], [[x.agent_label, x.category, x.count] for x in report.failures.heatmap])}
<p>LLM 归因：{escape(_availability(report.failures.llm_attribution))}<br>
盲检准确率：{escape(_availability(report.failures.review_accuracy, _pct(report.failures.review_accuracy_value)))}<br>
Cohen's κ：{escape(_availability(report.failures.kappa, f"{report.failures.kappa_value:.3f}" if report.failures.kappa_value is not None else None))}</p>
<h3>Top 失败案例</h3><div class="table-wrap"><table><thead><tr><th>运行</th><th>Agent</th><th>数据集</th><th>题目</th><th>类别</th><th>判定</th><th>制品</th></tr></thead><tbody>{top_rows}</tbody></table></div>
</main></body></html>"""


__all__ = ["render_html", "render_json", "render_markdown"]
