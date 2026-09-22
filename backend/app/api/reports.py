"""生成过的报告列表与下载（E7-T9）。

一次 `cli.report generate` 产出三行 `report_records`（HTML / Markdown / JSON），
列表把它们合并成一批（`app.report.listing.group_report_records`），下载端点
照抄 `task_runs.py::get_artifact` 那套：拿得到签名 URL 就 302，拿不到就流式转发，
**不把报告内容塞进 JSON**——HTML 报告本身就有几百 KB，塞进去和制品同理没必要。

这是个不要 token 的开放读接口：报告是给团队/答辩看的，不含官方补丁正文或
未公开的判定细节，跟 `task_runs.py` 的制品下载、`/tasks/{id}` 一个待遇。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import IO

from fastapi import APIRouter
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel

from app.api.deps import Page, PageDep, SessionDep, page_of
from app.api.errors import READ_ERROR_RESPONSES, not_found
from app.domain.enums import ReportFormat, ReportScope
from app.infrastructure.models.artifact import Artifact
from app.infrastructure.models.attribution import ReportRecord
from app.report.listing import list_report_batches
from app.storage import ArtifactNotFoundError, create_artifact_store, key_from_uri

router = APIRouter(prefix="/api/reports", tags=["reports"])

#: 和 task_runs.py 的制品下载用同一个块大小，方便对照。
_CHUNK_SIZE = 1024 * 1024


class ReportArtifactLink(BaseModel):
    format: ReportFormat
    report_record_id: int


class ReportBatchResponse(BaseModel):
    """一批报告（同一次生成的最多三种格式）。"""

    id: int
    scope: ReportScope
    run_ids: list[int]
    generated_at: datetime
    #: 每个实验号对应 "数据集@版本 · Agent 配置"；引用的实验被删掉了就不出现在这里
    dataset_labels: list[str]
    formats: list[ReportArtifactLink]


@router.get("", response_model=Page[ReportBatchResponse], responses=READ_ERROR_RESPONSES)
def list_reports(session: SessionDep, page: PageDep) -> Page[ReportBatchResponse]:
    """列出生成过的报告，按生成时间倒序，最新的在最前面。"""
    batches, labels, total = list_report_batches(session, limit=page.limit, offset=page.offset)
    items = [
        ReportBatchResponse(
            id=batch.id,
            scope=batch.scope,
            run_ids=list(batch.run_ids),
            generated_at=batch.generated_at,
            dataset_labels=[labels[run_id] for run_id in batch.run_ids if run_id in labels],
            formats=[
                ReportArtifactLink(format=report_format, report_record_id=record_id)
                for report_format, record_id in batch.formats
            ],
        )
        for batch in batches
    ]
    return page_of(items, total=total, params=page)


@router.get(
    "/{report_record_id}/download",
    response_model=None,
    responses={
        200: {"description": "报告内容（流式）", "content": {"*/*": {}}},
        302: {"description": "重定向到签名 URL"},
        **READ_ERROR_RESPONSES,
    },
)
def download_report(
    report_record_id: int, session: SessionDep
) -> RedirectResponse | StreamingResponse:
    """下载某一份报告制品。每个 `report_record_id` 已经唯一对应一种格式。"""
    record = session.get(ReportRecord, report_record_id)
    if record is None:
        raise not_found("REPORT_NOT_FOUND", f"找不到报告记录 #{report_record_id}")
    artifact = session.get(Artifact, record.artifact_id)
    if artifact is None:
        raise not_found("REPORT_NOT_FOUND", f"报告记录 #{report_record_id} 没有关联的制品")

    store = create_artifact_store()
    key = key_from_uri(artifact.uri)
    signed = store.url(key)
    if signed is not None:
        return RedirectResponse(signed, status_code=302)

    try:
        stream = store.open(key)
    except ArtifactNotFoundError as exc:
        raise not_found(
            "REPORT_FILE_MISSING",
            f"库里记着 {artifact.uri}，但制品存储里找不到这个文件",
        ) from exc

    filename = f"report-{report_record_id}.{record.format.value.lower()}"
    return StreamingResponse(
        _chunks(stream),
        media_type=artifact.content_type,
        headers={
            "Content-Length": str(artifact.size_bytes),
            "Content-Disposition": f'inline; filename="{filename}"',
            "X-Artifact-Sha256": artifact.sha256,
        },
    )


def _chunks(stream: IO[bytes]) -> Iterator[bytes]:
    try:
        while chunk := stream.read(_CHUNK_SIZE):
            yield chunk
    finally:
        stream.close()


__all__ = ["router"]
