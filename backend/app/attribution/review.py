"""人工盲检的抽样、批次身份和双人标注规则（E6-T3）。

这里放纯函数，不连接数据库。这样固定随机种子、双人一致与第三人仲裁这些
最容易写错的规则可以直接做单元测试，也不会和 HTTP 接口绑在一起。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.domain.enums import FailureCategory, HumanReviewAction

MINIMUM_PER_CATEGORY = 5
DEFAULT_SAMPLE_SIZE = 50
_BATCH_RE = re.compile(
    r"^review-v1-s(?P<seed>\d+)-a(?P<cutoff>\d+)-n(?P<target>\d+)-d(?P<digest>[0-9a-f]{32})$"
)


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    """一条可以进入抽检的自动归因快照。"""

    attribution_id: int
    task_run_id: int
    category: FailureCategory
    snapshot: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReviewBatchSpec:
    """可从批次号完整恢复的抽样参数。"""

    seed: int
    attribution_cutoff: int
    target_size: int
    digest: str

    @property
    def batch_id(self) -> str:
        return (
            f"review-v1-s{self.seed}-a{self.attribution_cutoff}-n{self.target_size}-d{self.digest}"
        )


class ReviewPhase(StrEnum):
    """一个案例目前需要谁处理。它不是数据库枚举，只用于接口状态。"""

    PRIMARY = "PRIMARY"
    ARBITRATION = "ARBITRATION"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True, slots=True)
class HumanLabel:
    """从一行 ``human_reviews`` 恢复出来的人工类别。"""

    reviewer: str
    category: FailureCategory


@dataclass(frozen=True, slots=True)
class ReviewResolution:
    """双人标注与仲裁的当前结果。"""

    phase: ReviewPhase
    final_category: FailureCategory | None


def stratified_sample(
    candidates: Sequence[ReviewCandidate],
    *,
    seed: int,
    target_size: int = DEFAULT_SAMPLE_SIZE,
    minimum_per_category: int = MINIMUM_PER_CATEGORY,
) -> tuple[ReviewCandidate, ...]:
    """按自动归因类别做可复现的分层抽样。

    每个已有类别先抽至少 ``minimum_per_category`` 条，不足就全取；再从各层
    剩余样本中补足总数。候选不足 ``target_size`` 时返回全部，绝不复制样本
    来凑数。输入先按主键排序，所以数据库返回顺序不会影响结果。
    """
    if seed < 0:
        raise ValueError("随机种子不能为负数")
    if target_size < 1:
        raise ValueError("抽检目标数必须大于 0")
    if minimum_per_category < 1:
        raise ValueError("每类最少样本数必须大于 0")

    grouped: dict[FailureCategory, list[ReviewCandidate]] = defaultdict(list)
    for candidate in sorted(candidates, key=lambda item: item.attribution_id):
        grouped[candidate.category].append(candidate)

    rng = random.Random(seed)
    selected: list[ReviewCandidate] = []
    leftovers: list[ReviewCandidate] = []
    for category in sorted(grouped, key=lambda item: item.value):
        rows = grouped[category][:]
        rng.shuffle(rows)
        take = min(minimum_per_category, len(rows))
        selected.extend(rows[:take])
        leftovers.extend(rows[take:])

    desired = min(len(candidates), max(target_size, len(selected)))
    rng.shuffle(leftovers)
    selected.extend(leftovers[: desired - len(selected)])
    rng.shuffle(selected)
    return tuple(selected)


def batch_digest(candidates: Iterable[ReviewCandidate]) -> str:
    """给抽中的自动归因快照做指纹，防止批次生成后结论被静默改写。"""
    payload = [
        {
            "attribution_id": item.attribution_id,
            "task_run_id": item.task_run_id,
            "category": item.category.value,
            "snapshot": item.snapshot,
        }
        for item in candidates
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()[:32]


def make_batch_spec(
    candidates: Sequence[ReviewCandidate],
    *,
    seed: int,
    target_size: int,
    attribution_cutoff: int,
) -> ReviewBatchSpec:
    """根据本次抽中的快照生成不超过 ``sample_batch_id`` 长度的批次号。"""
    return ReviewBatchSpec(
        seed=seed,
        attribution_cutoff=attribution_cutoff,
        target_size=target_size,
        digest=batch_digest(candidates),
    )


def parse_batch_id(batch_id: str) -> ReviewBatchSpec:
    """解析批次号；格式或版本不认识时明确拒绝。"""
    match = _BATCH_RE.fullmatch(batch_id)
    if match is None:
        raise ValueError("抽检批次号格式不正确")
    return ReviewBatchSpec(
        seed=int(match.group("seed")),
        attribution_cutoff=int(match.group("cutoff")),
        target_size=int(match.group("target")),
        digest=match.group("digest"),
    )


def label_from_review(
    *,
    reviewer: str,
    action: HumanReviewAction,
    corrected_category: FailureCategory | None,
    automatic_category: FailureCategory,
) -> HumanLabel | None:
    """把持久化动作还原成人工类别；纯备注不算一次标注。"""
    if action is HumanReviewAction.COMMENT:
        return None
    if action is HumanReviewAction.ACCEPT:
        return HumanLabel(reviewer=reviewer, category=automatic_category)
    if action is HumanReviewAction.MARK_TASK_DEFECT:
        return HumanLabel(reviewer=reviewer, category=FailureCategory.N2_TASK_DEFECT)
    if action is HumanReviewAction.CORRECT and corrected_category is not None:
        return HumanLabel(reviewer=reviewer, category=corrected_category)
    raise ValueError("人工复核记录缺少有效类别")


def resolve_labels(labels: Sequence[HumanLabel]) -> ReviewResolution:
    """前两人独立标注；不一致时只接受第三人的独立仲裁。"""
    if len(labels) < 2:
        return ReviewResolution(ReviewPhase.PRIMARY, None)
    if labels[0].category == labels[1].category:
        return ReviewResolution(ReviewPhase.COMPLETE, labels[0].category)
    if len(labels) < 3:
        return ReviewResolution(ReviewPhase.ARBITRATION, None)
    return ReviewResolution(ReviewPhase.COMPLETE, labels[2].category)


@dataclass(frozen=True, slots=True)
class LabelledReview:
    """一条能推出确定人工类别的标注（COMMENT 或缺失改判类别的 CORRECT 不算）。"""

    task_run_id: int
    reviewer: str
    human_category: str
    automatic_category: str


@dataclass(frozen=True, slots=True)
class ConfusionCell:
    """混淆矩阵里的一格：自动归因判了什么、人工判了什么、出现几次。"""

    automatic_category: str
    human_category: str
    count: int


@dataclass(frozen=True, slots=True)
class ReviewMetrics:
    """人工盲检的质量指标（E6-T4）：准确率、Cohen's kappa、混淆矩阵。

    κ 只在同一案例有两人独立标注时才算得出来；样本不够时对应字段为 None，
    原因写在 ``*_unavailable_reason`` 里，不能悄悄显示成 0。
    """

    sample_count: int
    accuracy: float | None
    accuracy_unavailable_reason: str | None
    kappa: float | None
    kappa_unavailable_reason: str | None
    confusion_matrix: tuple[ConfusionCell, ...]


def review_label(
    *, action: HumanReviewAction, corrected_category: FailureCategory | None, automatic: str
) -> str | None:
    """把一条 ``human_reviews`` 记录换算成人工类别字符串；COMMENT 或缺类别的 CORRECT 记 None。"""
    if action is HumanReviewAction.ACCEPT:
        return automatic
    if action is HumanReviewAction.MARK_TASK_DEFECT:
        return FailureCategory.N2_TASK_DEFECT.value
    if action is HumanReviewAction.CORRECT and corrected_category is not None:
        return corrected_category.value
    return None


def compute_review_metrics(labelled: Sequence[LabelledReview]) -> ReviewMetrics:
    """从一批已换算好人工类别的标注，算准确率 / κ / 混淆矩阵。不连数据库，可单测。"""
    if not labelled:
        reason = "没有可用于统计的人工盲检记录（E6-T3/E6-T4 未完成）"
        return ReviewMetrics(
            sample_count=0,
            accuracy=None,
            accuracy_unavailable_reason=reason,
            kappa=None,
            kappa_unavailable_reason=reason,
            confusion_matrix=(),
        )

    correct = sum(1 for item in labelled if item.human_category == item.automatic_category)
    accuracy = correct / len(labelled)

    confusion_counts: Counter[tuple[str, str]] = Counter(
        (item.automatic_category, item.human_category) for item in labelled
    )
    confusion_matrix = tuple(
        ConfusionCell(automatic_category=automatic, human_category=human, count=count)
        for (automatic, human), count in sorted(confusion_counts.items())
    )

    by_task: dict[int, list[str]] = defaultdict(list)
    for item in labelled:
        by_task[item.task_run_id].append(item.human_category)
    pairs = [(labels[0], labels[1]) for labels in by_task.values() if len(labels) >= 2]
    if not pairs:
        return ReviewMetrics(
            sample_count=len(labelled),
            accuracy=accuracy,
            accuracy_unavailable_reason=None,
            kappa=None,
            kappa_unavailable_reason="没有同一案例的双人标注，无法计算 κ",
            confusion_matrix=confusion_matrix,
        )
    observed = sum(1 for left, right in pairs if left == right) / len(pairs)
    left_counts = Counter(left for left, _ in pairs)
    right_counts = Counter(right for _, right in pairs)
    categories = set(left_counts) | set(right_counts)
    expected = sum(
        left_counts[category] / len(pairs) * right_counts[category] / len(pairs)
        for category in categories
    )
    kappa = 1.0 if expected == 1.0 and observed == 1.0 else (observed - expected) / (1 - expected)
    return ReviewMetrics(
        sample_count=len(labelled),
        accuracy=accuracy,
        accuracy_unavailable_reason=None,
        kappa=kappa,
        kappa_unavailable_reason=None,
        confusion_matrix=confusion_matrix,
    )


__all__ = [
    "DEFAULT_SAMPLE_SIZE",
    "MINIMUM_PER_CATEGORY",
    "ConfusionCell",
    "HumanLabel",
    "LabelledReview",
    "ReviewBatchSpec",
    "ReviewCandidate",
    "ReviewMetrics",
    "ReviewPhase",
    "ReviewResolution",
    "batch_digest",
    "compute_review_metrics",
    "label_from_review",
    "make_batch_spec",
    "parse_batch_id",
    "resolve_labels",
    "review_label",
    "stratified_sample",
]
