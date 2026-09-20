"""人工抽检队列与盲检接口（E6-T3）。

这组接口连 GET 也要求管理员 token。原因不只是写权限：复核详情会给人工标注者
官方补丁改动文件的摘要，而且提交后会返回自动归因。它们不能混进开放读接口。

盲检由响应模型保证：标注者提交有效类别之前，``automatic_attribution`` 为 None，
并通过 ``response_model_exclude_none`` 从 JSON 中彻底删掉，而不是交给前端 CSS 隐藏。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Body, Query
from pydantic import BaseModel, Field

from app.api.deps import AdminTokenDep, SessionDep
from app.api.errors import ERROR_RESPONSES, ApiError
from app.attribution.review import ReviewPhase, label_from_review
from app.attribution.review_service import (
    AutomaticAttribution,
    ReviewBatch,
    ReviewProgress,
    ReviewWorkflowError,
    automatic_for_reviewer,
    create_review_batch,
    list_review_queue,
    load_review_batch,
    review_progress,
    submit_review,
)
from app.domain.enums import (
    AgentOutcome,
    ArtifactKind,
    ArtifactOwnerType,
    AttributionStage,
    AttributionStatus,
    FailureCategory,
    HumanReviewAction,
    InfraOutcome,
    PatchKind,
    TaskDifficulty,
    TestRole,
    TestStatus,
)
from app.domain.patch_paths import derive_patch_paths
from app.infrastructure.models.agent import AgentConfig
from app.infrastructure.models.artifact import Artifact
from app.infrastructure.models.attribution import HumanReview
from app.infrastructure.models.benchmark import BenchmarkTask, Repository
from app.infrastructure.models.evaluation import (
    EvaluationRun,
    EvaluationTaskRun,
    PatchArtifact,
    TestResult,
)
from app.storage import create_artifact_store, key_from_uri

router = APIRouter(
    prefix="/api/review",
    tags=["review"],
    dependencies=[AdminTokenDep],
)


class ReviewBatchResponse(BaseModel):
    """抽检批次元数据。类别分层刻意不返回，避免队列侧漏。"""

    batch_id: str
    seed: int
    target_size: int
    selected_count: int
    eligible_count: int
    insufficient_pool: bool
    blind: bool = True


class ReviewQueueItemResponse(BaseModel):
    """队列中的一个案例，不含自动类别、理由、置信度或 evidence。"""

    position: int
    task_run_id: int
    task_id: str
    issue_title: str
    required_phase: ReviewPhase


class ReviewQueueResponse(BaseModel):
    batch: ReviewBatchResponse
    reviewer: str
    items: list[ReviewQueueItemResponse]
    pending_count: int


class GoldPatchSummary(BaseModel):
    """官方补丁只给人工看文件与规模，不返回代码正文。"""

    available: bool
    files: list[str]
    lines_added: int
    lines_deleted: int


class ReviewPatchSummary(BaseModel):
    kind: PatchKind
    files_changed: int
    lines_added: int
    lines_deleted: int
    is_empty: bool
    applies_cleanly: bool | None


class ReviewTestResult(BaseModel):
    test_id: str
    role: TestRole
    status: TestStatus
    duration_ms: int | None
    message_excerpt: str | None


class AutomaticAttributionResponse(BaseModel):
    """提交有效标签后才出现的自动归因对照。"""

    stage: AttributionStage
    category: FailureCategory
    secondary_category: FailureCategory | None
    confidence: Decimal | None
    judge_model: str | None
    prompt_hash: str | None
    evidence: dict[str, object]
    reasoning_zh: str | None
    status: AttributionStatus


class ReviewProgressResponse(BaseModel):
    phase: ReviewPhase
    label_count: int
    current_reviewer_submitted: bool
    final_category: FailureCategory | None


class ReviewCaseResponse(BaseModel):
    """盲检一屏三栏需要的全部证据。"""

    batch_id: str
    reviewer: str
    position: int
    task_run_id: int
    task_id: str
    issue_title: str
    issue_body: str
    repository: str
    difficulty: TaskDifficulty
    tags: list[str]
    agent_config_label: str
    infra_outcome: InfraOutcome | None
    agent_outcome: AgentOutcome | None
    gold_patch: GoldPatchSummary
    patches: list[ReviewPatchSummary]
    artifacts: list[ArtifactKind]
    tests: list[ReviewTestResult]
    progress: ReviewProgressResponse
    own_selected_category: FailureCategory | None = None
    automatic_attribution: AutomaticAttributionResponse | None = None


class ReviewSubmitRequest(BaseModel):
    """盲检者提交自己的类别；ACCEPT/CORRECT 由后端比较后派生。"""

    batch_id: str = Field(min_length=1, max_length=100)
    reviewer: str = Field(min_length=1, max_length=100)
    category: FailureCategory | None = None
    comment: str | None = Field(default=None, max_length=10_000)


class ReviewSubmitResponse(BaseModel):
    review_id: int
    action: HumanReviewAction
    progress: ReviewProgressResponse
    automatic_attribution: AutomaticAttributionResponse | None = None
    task_quarantined: bool


@router.get("/queue", response_model=ReviewQueueResponse, responses=ERROR_RESPONSES)
def get_review_queue(
    session: SessionDep,
    reviewer: Annotated[str, Query(min_length=1, max_length=100)],
    batch_id: Annotated[str | None, Query(max_length=100)] = None,
    seed: Annotated[int, Query(ge=0, le=9_223_372_036_854_775_807)] = 20260920,
    target_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ReviewQueueResponse:
    """新建或恢复抽检批次，并返回当前标注者尚未处理的案例。"""
    batch = _batch(session, batch_id=batch_id, seed=seed, target_size=target_size)
    try:
        items = list_review_queue(session, batch, reviewer=reviewer)
    except ReviewWorkflowError as exc:
        raise _api_error(exc) from exc
    return ReviewQueueResponse(
        batch=_batch_response(batch),
        reviewer=reviewer.strip(),
        items=[
            ReviewQueueItemResponse.model_validate(item, from_attributes=True) for item in items
        ],
        pending_count=len(items),
    )


@router.get(
    "/{task_run_id}",
    response_model=ReviewCaseResponse,
    response_model_exclude_none=True,
    responses=ERROR_RESPONSES,
)
def get_review_case(
    task_run_id: int,
    session: SessionDep,
    batch_id: Annotated[str, Query(min_length=1, max_length=100)],
    reviewer: Annotated[str, Query(min_length=1, max_length=100)],
) -> ReviewCaseResponse:
    """返回复核证据；自动归因只对已经提交有效类别的当前 reviewer 解锁。"""
    batch = _batch(session, batch_id=batch_id)
    try:
        progress = review_progress(session, batch, task_run_id=task_run_id, reviewer=reviewer)
        automatic = automatic_for_reviewer(
            session, batch, task_run_id=task_run_id, reviewer=reviewer
        )
    except ReviewWorkflowError as exc:
        raise _api_error(exc) from exc
    return _case_response(
        session,
        batch=batch,
        task_run_id=task_run_id,
        reviewer=reviewer.strip(),
        progress=progress,
        automatic=automatic,
    )


@router.post(
    "/{task_run_id}",
    response_model=ReviewSubmitResponse,
    response_model_exclude_none=True,
    responses=ERROR_RESPONSES,
)
def post_review(
    task_run_id: int,
    session: SessionDep,
    body: Annotated[ReviewSubmitRequest, Body()],
) -> ReviewSubmitResponse:
    """保存一条标签或备注。COMMENT 不算标注，也不会解锁自动答案。"""
    batch = _batch(session, batch_id=body.batch_id)
    try:
        result = submit_review(
            session,
            batch,
            task_run_id=task_run_id,
            reviewer=body.reviewer,
            category=body.category,
            comment=body.comment,
        )
        session.commit()
    except ReviewWorkflowError as exc:
        raise _api_error(exc) from exc
    return ReviewSubmitResponse(
        review_id=result.review_id,
        action=result.action,
        progress=_progress_response(result.progress),
        automatic_attribution=(
            _automatic_response(result.automatic) if result.automatic is not None else None
        ),
        task_quarantined=result.task_quarantined,
    )


def _batch(
    session: SessionDep,
    *,
    batch_id: str | None,
    seed: int = 20260920,
    target_size: int = 50,
) -> ReviewBatch:
    try:
        if batch_id is None:
            return create_review_batch(session, seed=seed, target_size=target_size)
        return load_review_batch(session, batch_id)
    except ReviewWorkflowError as exc:
        raise _api_error(exc) from exc


def _batch_response(batch: ReviewBatch) -> ReviewBatchResponse:
    return ReviewBatchResponse(
        batch_id=batch.spec.batch_id,
        seed=batch.spec.seed,
        target_size=batch.spec.target_size,
        selected_count=len(batch.candidates),
        eligible_count=batch.eligible_count,
        insufficient_pool=batch.eligible_count < batch.spec.target_size,
    )


def _case_response(
    session: SessionDep,
    *,
    batch: ReviewBatch,
    task_run_id: int,
    reviewer: str,
    progress: ReviewProgress,
    automatic: AutomaticAttribution | None,
) -> ReviewCaseResponse:
    candidate = next(item for item in batch.candidates if item.task_run_id == task_run_id)
    row = session.execute(
        sa.select(
            EvaluationTaskRun,
            BenchmarkTask,
            Repository.full_name,
            AgentConfig.label,
        )
        .join(BenchmarkTask, BenchmarkTask.id == EvaluationTaskRun.benchmark_task_id)
        .join(Repository, Repository.id == BenchmarkTask.repository_id)
        .join(EvaluationRun, EvaluationRun.id == EvaluationTaskRun.evaluation_run_id)
        .join(AgentConfig, AgentConfig.id == EvaluationRun.agent_config_id)
        .where(EvaluationTaskRun.id == task_run_id)
    ).one_or_none()
    if row is None:
        raise ApiError(404, "TASK_RUN_NOT_FOUND", f"找不到执行记录 #{task_run_id}")
    task_run, task, repository, agent_label = row
    patches = list(
        session.scalars(
            sa.select(PatchArtifact)
            .where(
                PatchArtifact.evaluation_task_run_id == task_run_id,
                PatchArtifact.kind.in_([PatchKind.AGENT_RAW, PatchKind.AGENT_NORMALIZED]),
            )
            .order_by(PatchArtifact.kind)
        )
    )
    artifact_kinds = list(
        session.scalars(
            sa.select(Artifact.kind)
            .where(
                Artifact.owner_type == ArtifactOwnerType.TASK_RUN,
                Artifact.owner_id == task_run_id,
            )
            .order_by(Artifact.kind)
        )
    )
    tests = list(
        session.scalars(
            sa.select(TestResult)
            .where(TestResult.evaluation_task_run_id == task_run_id)
            .order_by(TestResult.role, TestResult.test_id, TestResult.id)
        )
    )
    own_category = _own_category(
        session,
        batch_id=batch.spec.batch_id,
        task_run_id=task_run_id,
        reviewer=reviewer,
        automatic=candidate.category,
    )
    return ReviewCaseResponse(
        batch_id=batch.spec.batch_id,
        reviewer=reviewer,
        position=batch.candidates.index(candidate) + 1,
        task_run_id=task_run_id,
        task_id=task.task_id,
        issue_title=task.issue_title,
        issue_body=task.issue_body,
        repository=repository,
        difficulty=task.difficulty,
        tags=list(task.tags or []),
        agent_config_label=agent_label,
        infra_outcome=task_run.infra_outcome,
        agent_outcome=task_run.agent_outcome,
        gold_patch=_gold_patch_summary(task.raw_definition, task.gold_patch_uri),
        patches=[ReviewPatchSummary.model_validate(item, from_attributes=True) for item in patches],
        artifacts=artifact_kinds,
        tests=[ReviewTestResult.model_validate(item, from_attributes=True) for item in tests],
        progress=_progress_response(progress),
        own_selected_category=own_category,
        automatic_attribution=(_automatic_response(automatic) if automatic is not None else None),
    )


def _own_category(
    session: SessionDep,
    *,
    batch_id: str,
    task_run_id: int,
    reviewer: str,
    automatic: FailureCategory,
) -> FailureCategory | None:
    rows = session.scalars(
        sa.select(HumanReview)
        .where(
            HumanReview.sample_batch_id == batch_id,
            HumanReview.evaluation_task_run_id == task_run_id,
            HumanReview.reviewer == reviewer,
            HumanReview.action != HumanReviewAction.COMMENT,
        )
        .order_by(HumanReview.reviewed_at, HumanReview.id)
    )
    for row in rows:
        label = label_from_review(
            reviewer=row.reviewer,
            action=row.action,
            corrected_category=row.corrected_category,
            automatic_category=automatic,
        )
        if label is not None:
            return label.category
    return None


def _gold_patch_summary(
    raw_definition: dict[str, Any] | None, gold_patch_uri: str | None
) -> GoldPatchSummary:
    diff: str | None = None
    if isinstance(raw_definition, dict):
        value = raw_definition.get("gold_patch")
        if isinstance(value, str) and value.strip():
            diff = value
    if diff is None and gold_patch_uri:
        try:
            diff = (
                create_artifact_store()
                .get(key_from_uri(gold_patch_uri))
                .decode("utf-8", errors="replace")
            )
        except Exception:
            diff = None
    if not diff:
        return GoldPatchSummary(available=False, files=[], lines_added=0, lines_deleted=0)
    added = 0
    deleted = 0
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        added += int(line.startswith("+"))
        deleted += int(line.startswith("-"))
    return GoldPatchSummary(
        available=True,
        files=list(derive_patch_paths(diff)),
        lines_added=added,
        lines_deleted=deleted,
    )


def _automatic_response(value: AutomaticAttribution) -> AutomaticAttributionResponse:
    return AutomaticAttributionResponse.model_validate(value, from_attributes=True)


def _progress_response(value: ReviewProgress) -> ReviewProgressResponse:
    return ReviewProgressResponse.model_validate(value, from_attributes=True)


def _api_error(exc: ReviewWorkflowError) -> ApiError:
    if exc.code in {
        "REVIEW_BATCH_STALE",
        "REVIEWER_ALREADY_SUBMITTED",
        "REVIEW_ALREADY_COMPLETE",
    }:
        return ApiError(409, exc.code, exc.message)
    if exc.code in {"ATTRIBUTION_NOT_FOUND", "TASK_NOT_FOUND"}:
        return ApiError(404, exc.code, exc.message)
    return ApiError(422, exc.code, exc.message)


__all__ = ["router"]
