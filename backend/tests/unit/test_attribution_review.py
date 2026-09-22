"""E6-T3 抽样与双人标注规则。"""

from collections import Counter

import pytest

from app.attribution.review import (
    ConfusionCell,
    HumanLabel,
    LabelledReview,
    ReviewCandidate,
    ReviewPhase,
    batch_digest,
    compute_review_metrics,
    label_from_review,
    make_batch_spec,
    parse_batch_id,
    resolve_labels,
    review_label,
    stratified_sample,
)
from app.domain.enums import FailureCategory, HumanReviewAction


def _candidate(index: int, category: FailureCategory) -> ReviewCandidate:
    return ReviewCandidate(
        attribution_id=index,
        task_run_id=1000 + index,
        category=category,
        snapshot={"category": category.value, "reasoning_zh": f"理由 {index}"},
    )


def test_stratified_sample_takes_five_from_every_category_and_fifty_total() -> None:
    categories = [item for item in FailureCategory if item is not FailureCategory.N2_TASK_DEFECT]
    candidates = [
        _candidate(category_index * 20 + item_index + 1, category)
        for category_index, category in enumerate(categories)
        for item_index in range(10)
    ]

    selected = stratified_sample(candidates, seed=20260920)

    counts = Counter(item.category for item in selected)
    assert len(selected) == 50
    assert set(counts) == set(categories)
    assert all(count >= 5 for count in counts.values())
    assert len({item.task_run_id for item in selected}) == 50


def test_stratified_sample_takes_all_when_a_category_or_whole_pool_is_small() -> None:
    candidates = [
        *[
            _candidate(index, FailureCategory.F1_REQUIREMENT_MISUNDERSTANDING)
            for index in range(1, 4)
        ],
        *[_candidate(index, FailureCategory.F4_INCORRECT_LOGIC) for index in range(4, 9)],
    ]

    selected = stratified_sample(candidates, seed=7)

    assert {item.attribution_id for item in selected} == set(range(1, 9))


def test_same_seed_and_unsorted_input_produce_same_batch_order() -> None:
    candidates = [_candidate(index, FailureCategory.F3_INCOMPLETE_FIX) for index in range(1, 70)]

    left = stratified_sample(candidates, seed=42)
    right = stratified_sample(list(reversed(candidates)), seed=42)

    assert [item.task_run_id for item in left] == [item.task_run_id for item in right]


def test_batch_id_round_trips_and_detects_snapshot_change() -> None:
    selected = tuple(
        _candidate(index, FailureCategory.F2_WRONG_FILE_LOCALIZATION) for index in range(1, 6)
    )
    spec = make_batch_spec(
        selected,
        seed=20260920,
        target_size=50,
        attribution_cutoff=99,
    )

    assert parse_batch_id(spec.batch_id) == spec
    changed = [*selected[:-1], _candidate(5, FailureCategory.F4_INCORRECT_LOGIC)]
    assert batch_digest(changed) != spec.digest


def test_two_equal_labels_finish_and_disagreement_needs_a_third() -> None:
    f3 = FailureCategory.F3_INCOMPLETE_FIX
    f4 = FailureCategory.F4_INCORRECT_LOGIC

    assert resolve_labels([HumanLabel("a", f3)]).phase is ReviewPhase.PRIMARY
    agreed = resolve_labels([HumanLabel("a", f3), HumanLabel("b", f3)])
    conflict = resolve_labels([HumanLabel("a", f3), HumanLabel("b", f4)])
    arbitrated = resolve_labels([HumanLabel("a", f3), HumanLabel("b", f4), HumanLabel("c", f4)])

    assert agreed.phase is ReviewPhase.COMPLETE
    assert agreed.final_category is f3
    assert conflict.phase is ReviewPhase.ARBITRATION
    assert conflict.final_category is None
    assert arbitrated.phase is ReviewPhase.COMPLETE
    assert arbitrated.final_category is f4


@pytest.mark.parametrize(
    ("action", "corrected", "expected"),
    [
        (HumanReviewAction.ACCEPT, None, FailureCategory.F1_REQUIREMENT_MISUNDERSTANDING),
        (
            HumanReviewAction.CORRECT,
            FailureCategory.F4_INCORRECT_LOGIC,
            FailureCategory.F4_INCORRECT_LOGIC,
        ),
        (
            HumanReviewAction.MARK_TASK_DEFECT,
            FailureCategory.N2_TASK_DEFECT,
            FailureCategory.N2_TASK_DEFECT,
        ),
        (HumanReviewAction.COMMENT, None, None),
    ],
)
def test_persisted_actions_restore_the_human_label(
    action: HumanReviewAction,
    corrected: FailureCategory | None,
    expected: FailureCategory | None,
) -> None:
    label = label_from_review(
        reviewer="alice",
        action=action,
        corrected_category=corrected,
        automatic_category=FailureCategory.F1_REQUIREMENT_MISUNDERSTANDING,
    )

    assert (label.category if label is not None else None) is expected


@pytest.mark.parametrize(
    ("action", "corrected", "expected"),
    [
        (HumanReviewAction.ACCEPT, None, "F1_REQUIREMENT_MISUNDERSTANDING"),
        (HumanReviewAction.CORRECT, FailureCategory.F4_INCORRECT_LOGIC, "F4_INCORRECT_LOGIC"),
        (HumanReviewAction.MARK_TASK_DEFECT, None, "N2_TASK_DEFECT"),
        (HumanReviewAction.CORRECT, None, None),  # 改判但没给类别，算不出人工类别
        (HumanReviewAction.COMMENT, None, None),
    ],
)
def test_review_label_matches_e6_t4_report_semantics(
    action: HumanReviewAction,
    corrected: FailureCategory | None,
    expected: str | None,
) -> None:
    """`review_label` 是原来报告口径里的私有函数，只是挪了地方（每条单独计，不做仲裁）。"""
    assert (
        review_label(
            action=action, corrected_category=corrected, automatic="F1_REQUIREMENT_MISUNDERSTANDING"
        )
        == expected
    )


def test_compute_review_metrics_reports_unavailable_without_any_labelled_review() -> None:
    metrics = compute_review_metrics([])

    assert metrics.sample_count == 0
    assert metrics.accuracy is None
    assert metrics.accuracy_unavailable_reason is not None
    assert metrics.kappa is None
    assert metrics.kappa_unavailable_reason is not None
    assert metrics.confusion_matrix == ()


def test_compute_review_metrics_accuracy_and_confusion_matrix_without_double_labelling() -> None:
    f1, f4 = "F1_REQUIREMENT_MISUNDERSTANDING", "F4_INCORRECT_LOGIC"
    labelled = [
        LabelledReview(1, "alice", f1, f1),
        LabelledReview(2, "alice", f4, f1),
        LabelledReview(3, "alice", f4, f4),
    ]

    metrics = compute_review_metrics(labelled)

    assert metrics.sample_count == 3
    assert metrics.accuracy == pytest.approx(2 / 3)
    assert metrics.kappa is None
    assert metrics.kappa_unavailable_reason == "没有同一案例的双人标注，无法计算 κ"
    assert set(metrics.confusion_matrix) == {
        ConfusionCell("F1_REQUIREMENT_MISUNDERSTANDING", "F1_REQUIREMENT_MISUNDERSTANDING", 1),
        ConfusionCell("F1_REQUIREMENT_MISUNDERSTANDING", "F4_INCORRECT_LOGIC", 1),
        ConfusionCell("F4_INCORRECT_LOGIC", "F4_INCORRECT_LOGIC", 1),
    }


def test_compute_review_metrics_kappa_from_double_labelled_cases() -> None:
    # 4 个案例都由两人标注：3 个一致（F1×2、F4、N2 各一次一致），1 个不一致——
    # 用最朴素的方式手算期望值，回归测试用，不是"背下 Cohen's kappa 公式"。
    f1, f4, n2 = (
        "F1_REQUIREMENT_MISUNDERSTANDING",
        "F4_INCORRECT_LOGIC",
        "N2_TASK_DEFECT",
    )
    labelled = [
        LabelledReview(1, "alice", f1, f1),
        LabelledReview(1, "bob", f1, f1),
        LabelledReview(2, "alice", f4, f4),
        LabelledReview(2, "bob", f4, f4),
        LabelledReview(3, "alice", n2, f1),
        LabelledReview(3, "bob", n2, f1),
        LabelledReview(4, "alice", f1, f4),
        LabelledReview(4, "bob", f4, f4),
    ]

    metrics = compute_review_metrics(labelled)

    observed = 3 / 4  # 4 对里 3 对一致
    # alice: f1 f4 n2 f1 → f1:2 f4:1 n2:1；bob: f1 f4 n2 f4 → f1:1 f4:2 n2:1
    expected_chance = (2 / 4 * 1 / 4) + (1 / 4 * 2 / 4) + (1 / 4 * 1 / 4)
    expected_kappa = (observed - expected_chance) / (1 - expected_chance)

    assert metrics.kappa == pytest.approx(expected_kappa)
    assert metrics.kappa_unavailable_reason is None
