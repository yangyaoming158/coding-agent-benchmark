"""人工盲检的抽样、批次身份和双人标注规则（E6-T3）。

这里放纯函数，不连接数据库。这样固定随机种子、双人一致与第三人仲裁这些
最容易写错的规则可以直接做单元测试，也不会和 HTTP 接口绑在一起。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections import defaultdict
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


__all__ = [
    "DEFAULT_SAMPLE_SIZE",
    "MINIMUM_PER_CATEGORY",
    "HumanLabel",
    "ReviewBatchSpec",
    "ReviewCandidate",
    "ReviewPhase",
    "ReviewResolution",
    "batch_digest",
    "label_from_review",
    "make_batch_spec",
    "parse_batch_id",
    "resolve_labels",
    "stratified_sample",
]
