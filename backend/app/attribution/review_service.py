"""人工盲检的查库与落库（E6-T3）。

批次本身不新增表：固定种子、归因行截止 ID、目标样本数和自动归因快照指纹
编码进 ``sample_batch_id``。只要批次号相同，抽中的执行记录和顺序就相同；
有人在标注期间重跑自动归因时，指纹校验会拒绝继续写入，不会静默换答案。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.attribution.review import (
    DEFAULT_SAMPLE_SIZE,
    HumanLabel,
    LabelledReview,
    ReviewBatchSpec,
    ReviewCandidate,
    ReviewMetrics,
    ReviewPhase,
    ReviewResolution,
    batch_digest,
    compute_review_metrics,
    label_from_review,
    make_batch_spec,
    parse_batch_id,
    resolve_labels,
    review_label,
    stratified_sample,
)
from app.domain.enums import (
    AttributionStage,
    AttributionStatus,
    FailureCategory,
    HumanReviewAction,
    TaskValidationState,
)
from app.infrastructure.models.attribution import FailureAttribution, HumanReview
from app.infrastructure.models.benchmark import BenchmarkTask
from app.infrastructure.models.evaluation import EvaluationTaskRun


class ReviewWorkflowError(Exception):
    """抽检工作流的可预期错误，由 API 层转换成稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ReviewBatch:
    """一次已经固定成员与顺序的抽检批次。"""

    spec: ReviewBatchSpec
    candidates: tuple[ReviewCandidate, ...]
    eligible_count: int

    @property
    def category_count(self) -> int:
        return len({item.category for item in self.candidates})


@dataclass(frozen=True, slots=True)
class ReviewQueueItem:
    """队列里的一行。刻意不携带任何自动归因内容。"""

    position: int
    task_run_id: int
    task_id: str
    issue_title: str
    required_phase: ReviewPhase


@dataclass(frozen=True, slots=True)
class AutomaticAttribution:
    """标注者提交有效分类后才允许返回的自动归因。"""

    stage: AttributionStage
    category: FailureCategory
    secondary_category: FailureCategory | None
    confidence: Decimal | None
    judge_model: str | None
    prompt_hash: str | None
    evidence: dict[str, object]
    reasoning_zh: str | None
    status: AttributionStatus


@dataclass(frozen=True, slots=True)
class ReviewProgress:
    """一个案例的标注进度，不包含其他标注者选择了什么。"""

    phase: ReviewPhase
    label_count: int
    current_reviewer_submitted: bool
    final_category: FailureCategory | None


@dataclass(frozen=True, slots=True)
class ReviewSubmission:
    """一次提交后的结果。"""

    review_id: int
    action: HumanReviewAction
    progress: ReviewProgress
    automatic: AutomaticAttribution | None
    task_quarantined: bool


def create_review_batch(
    session: Session, *, seed: int, target_size: int = DEFAULT_SAMPLE_SIZE
) -> ReviewBatch:
    """从当前自动归因生成批次；候选不足目标数时全取。"""
    candidates = _load_candidates(session)
    cutoff = max((item.attribution_id for item in candidates), default=0)
    selected = stratified_sample(candidates, seed=seed, target_size=target_size)
    spec = make_batch_spec(
        selected,
        seed=seed,
        target_size=target_size,
        attribution_cutoff=cutoff,
    )
    return ReviewBatch(spec=spec, candidates=selected, eligible_count=len(candidates))


def load_review_batch(session: Session, batch_id: str) -> ReviewBatch:
    """按批次号重建同一批案例，并验证自动归因没有变化。"""
    try:
        spec = parse_batch_id(batch_id)
    except ValueError as exc:
        raise ReviewWorkflowError("INVALID_REVIEW_BATCH", str(exc)) from exc
    candidates = _load_candidates(session, attribution_cutoff=spec.attribution_cutoff)
    selected = stratified_sample(candidates, seed=spec.seed, target_size=spec.target_size)
    if batch_digest(selected) != spec.digest:
        raise ReviewWorkflowError(
            "REVIEW_BATCH_STALE",
            "这批案例生成后自动归因发生了变化，请保留已有记录并新建抽检批次",
        )
    return ReviewBatch(spec=spec, candidates=selected, eligible_count=len(candidates))


def list_review_queue(
    session: Session, batch: ReviewBatch, *, reviewer: str
) -> list[ReviewQueueItem]:
    """列出该标注者现在能处理的案例，不泄露分层类别。"""
    reviewer = _validate_reviewer(reviewer)
    task_run_ids = [item.task_run_id for item in batch.candidates]
    reviews = _reviews_by_task(session, batch.spec.batch_id, task_run_ids)
    facts = {
        row.task_run_id: (row.task_id, row.issue_title)
        for row in session.execute(
            sa.select(
                EvaluationTaskRun.id.label("task_run_id"),
                BenchmarkTask.task_id,
                BenchmarkTask.issue_title,
            )
            .join(BenchmarkTask, BenchmarkTask.id == EvaluationTaskRun.benchmark_task_id)
            .where(EvaluationTaskRun.id.in_(task_run_ids))
        )
    }

    queue: list[ReviewQueueItem] = []
    for position, candidate in enumerate(batch.candidates, start=1):
        labels = _labels(reviews[candidate.task_run_id], candidate.category)
        resolution = resolve_labels(labels)
        if any(item.reviewer == reviewer for item in labels):
            continue
        if resolution.phase is ReviewPhase.COMPLETE:
            continue
        task_id, issue_title = facts[candidate.task_run_id]
        queue.append(
            ReviewQueueItem(
                position=position,
                task_run_id=candidate.task_run_id,
                task_id=task_id,
                issue_title=issue_title,
                required_phase=resolution.phase,
            )
        )
    return queue


def review_progress(
    session: Session, batch: ReviewBatch, *, task_run_id: int, reviewer: str
) -> ReviewProgress:
    """返回一个案例的进度；只显示人数和最终结论，不显示他人的中间选择。"""
    reviewer = _validate_reviewer(reviewer)
    candidate = candidate_of(batch, task_run_id)
    rows = _review_rows(session, batch.spec.batch_id, task_run_id)
    labels = _labels(rows, candidate.category)
    resolution = resolve_labels(labels)
    return ReviewProgress(
        phase=resolution.phase,
        label_count=len(labels),
        current_reviewer_submitted=any(item.reviewer == reviewer for item in labels),
        final_category=resolution.final_category,
    )


def automatic_for_reviewer(
    session: Session, batch: ReviewBatch, *, task_run_id: int, reviewer: str
) -> AutomaticAttribution | None:
    """只有当前标注者交过有效类别才返回自动归因；COMMENT 不解锁答案。"""
    progress = review_progress(session, batch, task_run_id=task_run_id, reviewer=reviewer)
    if not progress.current_reviewer_submitted:
        return None
    attribution = session.scalar(
        sa.select(FailureAttribution).where(
            FailureAttribution.evaluation_task_run_id == task_run_id
        )
    )
    if attribution is None:  # 批次指纹正常时不应发生，仍给出可读错误
        raise ReviewWorkflowError("ATTRIBUTION_NOT_FOUND", "这次执行没有自动归因")
    return _automatic(attribution)


def submit_review(
    session: Session,
    batch: ReviewBatch,
    *,
    task_run_id: int,
    reviewer: str,
    category: FailureCategory | None,
    comment: str | None,
) -> ReviewSubmission:
    """原子保存一次盲检标注，并在最终结论为 N2 时隔离题目。

    API 接收的是标注者自己选择的类别，不让盲检者直接填 ACCEPT/CORRECT；
    后端和隐藏的自动类别比较后再派生这两个动作。
    """
    reviewer = _validate_reviewer(reviewer)
    comment = comment.strip() if comment and comment.strip() else None
    candidate = candidate_of(batch, task_run_id)

    attribution = session.scalar(
        sa.select(FailureAttribution)
        .where(FailureAttribution.id == candidate.attribution_id)
        .with_for_update()
    )
    if attribution is None:
        raise ReviewWorkflowError("ATTRIBUTION_NOT_FOUND", "这次执行没有自动归因")
    locked_snapshot = _candidate(attribution)
    if (
        locked_snapshot.category is not candidate.category
        or locked_snapshot.snapshot != candidate.snapshot
    ):
        raise ReviewWorkflowError(
            "REVIEW_BATCH_STALE",
            "锁定案例时发现自动归因已变化，请保留已有记录并新建抽检批次",
        )

    rows = _review_rows(session, batch.spec.batch_id, task_run_id)
    labels = _labels(rows, candidate.category)

    if category is None:
        if comment is None:
            raise ReviewWorkflowError("COMMENT_REQUIRED", "只写备注时 comment 不能为空")
        review = HumanReview(
            evaluation_task_run_id=task_run_id,
            reviewer=reviewer,
            sample_batch_id=batch.spec.batch_id,
            blind=True,
            action=HumanReviewAction.COMMENT,
            comment=comment,
        )
        session.add(review)
        session.flush()
        resolution = resolve_labels(labels)
        return ReviewSubmission(
            review_id=review.id,
            action=review.action,
            progress=_progress(resolution, labels, reviewer),
            automatic=None,
            task_quarantined=False,
        )

    if any(item.reviewer == reviewer for item in labels):
        raise ReviewWorkflowError(
            "REVIEWER_ALREADY_SUBMITTED", "同一标注者不能重复提交这个案例的类别"
        )
    before = resolve_labels(labels)
    if before.phase is ReviewPhase.COMPLETE:
        raise ReviewWorkflowError("REVIEW_ALREADY_COMPLETE", "这个案例已经完成复核")
    if category is FailureCategory.N2_TASK_DEFECT and comment is None:
        raise ReviewWorkflowError("TASK_DEFECT_REASON_REQUIRED", "标记题目缺陷时必须填写具体理由")

    action, corrected = _action_for(category, candidate.category)
    review = HumanReview(
        evaluation_task_run_id=task_run_id,
        reviewer=reviewer,
        sample_batch_id=batch.spec.batch_id,
        blind=True,
        action=action,
        corrected_category=corrected,
        comment=comment,
    )
    session.add(review)
    session.flush()

    labels = [*labels, HumanLabel(reviewer=reviewer, category=category)]
    resolution = resolve_labels(labels)
    quarantined = False
    if resolution.final_category is FailureCategory.N2_TASK_DEFECT:
        quarantined = _quarantine_task(
            session,
            task_run_id=task_run_id,
            batch_id=batch.spec.batch_id,
            labels=labels,
            reason=comment or "双人复核确认题目存在缺陷",
        )
    return ReviewSubmission(
        review_id=review.id,
        action=review.action,
        progress=_progress(resolution, labels, reviewer),
        automatic=_automatic(attribution),
        task_quarantined=quarantined,
    )


def candidate_of(batch: ReviewBatch, task_run_id: int) -> ReviewCandidate:
    """确认执行记录属于这批抽检，防止拿批次号提交任意运行。"""
    for candidate in batch.candidates:
        if candidate.task_run_id == task_run_id:
            return candidate
    raise ReviewWorkflowError("CASE_NOT_IN_BATCH", "这次执行不在指定的抽检批次中")


def _load_candidates(
    session: Session, *, attribution_cutoff: int | None = None
) -> tuple[ReviewCandidate, ...]:
    stmt = (
        sa.select(FailureAttribution)
        .join(
            EvaluationTaskRun,
            EvaluationTaskRun.id == FailureAttribution.evaluation_task_run_id,
        )
        .where(
            FailureAttribution.stage.in_([AttributionStage.RULE, AttributionStage.LLM]),
            FailureAttribution.status.in_([AttributionStatus.OK, AttributionStatus.NEEDS_HUMAN]),
            EvaluationTaskRun.is_canonical.is_(True),
        )
        .order_by(FailureAttribution.id)
    )
    if attribution_cutoff is not None:
        stmt = stmt.where(FailureAttribution.id <= attribution_cutoff)
    return tuple(_candidate(row) for row in session.scalars(stmt))


def _candidate(row: FailureAttribution) -> ReviewCandidate:
    snapshot: dict[str, Any] = {
        "stage": row.stage.value,
        "category": row.category.value,
        "secondary_category": (
            row.secondary_category.value if row.secondary_category is not None else None
        ),
        "confidence": str(row.confidence) if row.confidence is not None else None,
        "judge_model": row.judge_model,
        "prompt_hash": row.prompt_hash,
        "evidence": row.evidence,
        "reasoning_zh": row.reasoning_zh,
        "status": row.status.value,
    }
    return ReviewCandidate(
        attribution_id=row.id,
        task_run_id=row.evaluation_task_run_id,
        category=row.category,
        snapshot=snapshot,
    )


def _reviews_by_task(
    session: Session, batch_id: str, task_run_ids: Sequence[int]
) -> dict[int, list[HumanReview]]:
    grouped: dict[int, list[HumanReview]] = defaultdict(list)
    if not task_run_ids:
        return grouped
    rows = session.scalars(
        sa.select(HumanReview)
        .where(
            HumanReview.sample_batch_id == batch_id,
            HumanReview.evaluation_task_run_id.in_(list(task_run_ids)),
        )
        .order_by(HumanReview.reviewed_at, HumanReview.id)
    )
    for row in rows:
        grouped[row.evaluation_task_run_id].append(row)
    return grouped


def _review_rows(session: Session, batch_id: str, task_run_id: int) -> list[HumanReview]:
    return list(
        session.scalars(
            sa.select(HumanReview)
            .where(
                HumanReview.sample_batch_id == batch_id,
                HumanReview.evaluation_task_run_id == task_run_id,
            )
            .order_by(HumanReview.reviewed_at, HumanReview.id)
        )
    )


def _labels(rows: Sequence[HumanReview], automatic: FailureCategory) -> list[HumanLabel]:
    labels: list[HumanLabel] = []
    for row in rows:
        label = label_from_review(
            reviewer=row.reviewer,
            action=row.action,
            corrected_category=row.corrected_category,
            automatic_category=automatic,
        )
        if label is not None:
            labels.append(label)
    return labels


def _action_for(
    selected: FailureCategory, automatic: FailureCategory
) -> tuple[HumanReviewAction, FailureCategory | None]:
    if selected is FailureCategory.N2_TASK_DEFECT:
        return HumanReviewAction.MARK_TASK_DEFECT, FailureCategory.N2_TASK_DEFECT
    if selected is automatic:
        return HumanReviewAction.ACCEPT, None
    return HumanReviewAction.CORRECT, selected


def _automatic(row: FailureAttribution) -> AutomaticAttribution:
    return AutomaticAttribution(
        stage=row.stage,
        category=row.category,
        secondary_category=row.secondary_category,
        confidence=row.confidence,
        judge_model=row.judge_model,
        prompt_hash=row.prompt_hash,
        evidence=row.evidence,
        reasoning_zh=row.reasoning_zh,
        status=row.status,
    )


def _progress(
    resolution: ReviewResolution, labels: Sequence[HumanLabel], reviewer: str
) -> ReviewProgress:
    return ReviewProgress(
        phase=resolution.phase,
        label_count=len(labels),
        current_reviewer_submitted=any(item.reviewer == reviewer for item in labels),
        final_category=resolution.final_category,
    )


def _quarantine_task(
    session: Session,
    *,
    task_run_id: int,
    batch_id: str,
    labels: Sequence[HumanLabel],
    reason: str,
) -> bool:
    task = session.scalar(
        sa.select(BenchmarkTask)
        .join(EvaluationTaskRun, EvaluationTaskRun.benchmark_task_id == BenchmarkTask.id)
        .where(EvaluationTaskRun.id == task_run_id)
        .with_for_update()
    )
    if task is None:
        raise ReviewWorkflowError("TASK_NOT_FOUND", "找不到这次执行对应的题目")
    if task.validation_state is TaskValidationState.QUARANTINED:
        return False
    before = task.validation_state
    task.validation_state = TaskValidationState.QUARANTINED
    task.quarantine = {
        "at": datetime.now(UTC).isoformat(),
        "from_state": before.value,
        "reason": reason,
        "source": "human_review",
        "sample_batch_id": batch_id,
        "reviewers": [item.reviewer for item in labels],
    }
    return True


def _validate_reviewer(reviewer: str) -> str:
    reviewer = reviewer.strip()
    if not reviewer:
        raise ReviewWorkflowError("REVIEWER_REQUIRED", "reviewer 不能为空")
    if len(reviewer) > 100:
        raise ReviewWorkflowError("REVIEWER_TOO_LONG", "reviewer 最多 100 个字符")
    return reviewer


def category_distribution(batch: ReviewBatch) -> dict[str, int]:
    """只给后台测试和后续统计使用；盲检队列接口禁止返回这份数据。"""
    return dict(sorted(Counter(item.category.value for item in batch.candidates).items()))


def labelled_reviews(
    session: Session,
    *,
    run_ids: Sequence[int] | None = None,
    batch_id: str | None = None,
) -> list[LabelledReview]:
    """查出能推出确定人工类别的标注（COMMENT 和缺类别的 CORRECT 不算）。

    ``run_ids`` 给实验报告用（E6-T4 前就有的口径，按 ``evaluation_run_id`` 过滤）；
    ``batch_id`` 给抽检质量看板用（不挂在某次实验下，按抽检批次过滤）。两个都不给
    就统计全库，两个都给则同时满足。
    """
    stmt = (
        sa.select(HumanReview, FailureAttribution.category)
        .join(
            FailureAttribution,
            FailureAttribution.evaluation_task_run_id == HumanReview.evaluation_task_run_id,
        )
        .order_by(HumanReview.evaluation_task_run_id, HumanReview.reviewed_at, HumanReview.id)
    )
    if run_ids is not None:
        stmt = stmt.join(
            EvaluationTaskRun, EvaluationTaskRun.id == HumanReview.evaluation_task_run_id
        ).where(EvaluationTaskRun.evaluation_run_id.in_(list(run_ids)))
    if batch_id is not None:
        stmt = stmt.where(HumanReview.sample_batch_id == batch_id)
    rows = session.execute(stmt).all()
    labelled: list[LabelledReview] = []
    for review, automatic_category in rows:
        automatic = automatic_category.value
        human = review_label(
            action=review.action,
            corrected_category=review.corrected_category,
            automatic=automatic,
        )
        if human is None:
            continue
        labelled.append(
            LabelledReview(
                task_run_id=review.evaluation_task_run_id,
                reviewer=review.reviewer,
                human_category=human,
                automatic_category=automatic,
            )
        )
    return labelled


def review_metrics(
    session: Session,
    *,
    run_ids: Sequence[int] | None = None,
    batch_id: str | None = None,
) -> ReviewMetrics:
    """查库 + 算指标的入口；口径全在 :func:`compute_review_metrics` 这个纯函数里。"""
    return compute_review_metrics(labelled_reviews(session, run_ids=run_ids, batch_id=batch_id))


__all__ = [
    "AutomaticAttribution",
    "ReviewBatch",
    "ReviewProgress",
    "ReviewQueueItem",
    "ReviewSubmission",
    "ReviewWorkflowError",
    "automatic_for_reviewer",
    "candidate_of",
    "category_distribution",
    "create_review_batch",
    "labelled_reviews",
    "list_review_queue",
    "load_review_batch",
    "review_metrics",
    "review_progress",
    "submit_review",
]
