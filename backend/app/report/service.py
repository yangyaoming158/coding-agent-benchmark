"""报告生成与制品登记服务。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.domain.enums import ArtifactKind, ArtifactOwnerType, ReportFormat, ReportScope
from app.infrastructure.models.artifact import Artifact
from app.infrastructure.models.attribution import ReportRecord
from app.report.aggregate import build_report
from app.report.models import ReportData
from app.report.render import render_html, render_json, render_markdown
from app.storage import ArtifactRef, ArtifactStore


@dataclass(frozen=True, slots=True)
class GeneratedReport:
    """一次生成产生的统一数据和三份制品索引。"""

    data: ReportData
    artifacts: dict[ReportFormat, ArtifactRef]
    record_ids: dict[ReportFormat, int]


_FORMATS = (
    (
        ReportFormat.HTML,
        ArtifactKind.REPORT_HTML,
        "report.html",
        "text/html; charset=utf-8",
        render_html,
    ),
    (
        ReportFormat.MARKDOWN,
        ArtifactKind.REPORT_MARKDOWN,
        "report.md",
        "text/markdown; charset=utf-8",
        render_markdown,
    ),
    (
        ReportFormat.JSON,
        ArtifactKind.REPORT_JSON,
        "report.json",
        "application/json; charset=utf-8",
        render_json,
    ),
)


def persist_report(
    session: Session,
    store: ArtifactStore,
    report: ReportData,
    *,
    params: dict[str, object] | None = None,
) -> GeneratedReport:
    """把三种格式写入制品库并登记 ``artifacts`` / ``report_records``。

    三种文件共用生成时间目录，因此同一次生成可整体识别。文件不压缩，HTML 才能
    直接打开；日志等大制品仍沿用存储层默认的 gzip 策略。
    """
    anchor_run_id = report.run_ids[0]
    stamp = report.generated_at.strftime("%Y%m%dT%H%M%S%fZ")
    prefix = f"runs/{anchor_run_id}/reports/{stamp}"
    scope = ReportScope(report.scope)
    refs: dict[ReportFormat, ArtifactRef] = {}
    records: dict[ReportFormat, int] = {}
    written_keys: list[str] = []
    metadata = dict(params or {})
    metadata["schema_version"] = report.schema_version

    try:
        for report_format, artifact_kind, filename, content_type, renderer in _FORMATS:
            key = f"{prefix}/{filename}"
            ref = store.put(
                key,
                renderer(report).encode("utf-8"),
                content_type=content_type,
                compress=False,
            )
            written_keys.append(key)
            artifact = Artifact(
                owner_type=ArtifactOwnerType.EVAL_RUN,
                owner_id=anchor_run_id,
                kind=artifact_kind,
                uri=ref.uri,
                backend=ref.backend,
                content_type=ref.content_type,
                size_bytes=ref.size_bytes,
                sha256=ref.sha256,
                compressed=ref.compressed,
            )
            session.add(artifact)
            session.flush()
            record = ReportRecord(
                evaluation_run_id=anchor_run_id,
                scope=scope,
                run_ids=list(report.run_ids),
                format=report_format,
                artifact_id=artifact.id,
                params=metadata,
                generated_at=report.generated_at,
            )
            session.add(record)
            session.flush()
            refs[report_format] = ref
            records[report_format] = record.id
    except Exception:
        for key in written_keys:
            store.delete(key)
        raise
    return GeneratedReport(data=report, artifacts=refs, record_ids=records)


def generate_report(
    session: Session,
    store: ArtifactStore,
    run_ids: list[int],
    *,
    title: str = "AI Coding Agent 评测对比报告",
    base_url: str = "http://localhost:8000",
    top_n: int = 10,
    host_metrics_csv: Path | None = None,
) -> GeneratedReport:
    """聚合已有实验数据并持久化 HTML、Markdown、JSON 三种报告。"""
    report = build_report(
        session,
        run_ids,
        title=title,
        base_url=base_url,
        top_n=top_n,
        host_metrics_csv=host_metrics_csv,
    )
    return persist_report(
        session,
        store,
        report,
        params={
            "title": title,
            "base_url": base_url,
            "top_n": top_n,
            "host_metrics_csv": str(host_metrics_csv) if host_metrics_csv else None,
        },
    )


__all__ = ["GeneratedReport", "generate_report", "persist_report"]
