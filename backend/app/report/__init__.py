"""报表：统一聚合并生成 HTML、Markdown、JSON 制品。"""

from app.report.aggregate import ReportInputError, build_report
from app.report.render import render_html, render_json, render_markdown
from app.report.service import GeneratedReport, generate_report, persist_report

__all__ = [
    "GeneratedReport",
    "ReportInputError",
    "build_report",
    "generate_report",
    "persist_report",
    "render_html",
    "render_json",
    "render_markdown",
]
