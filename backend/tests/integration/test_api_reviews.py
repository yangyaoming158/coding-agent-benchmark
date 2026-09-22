"""E6-T3 抽检队列、盲检响应、双人标注和仲裁 API。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.attribution.persistence import save_rule_verdicts
from app.attribution.rules import RuleName, RuleVerdict
from app.domain.enums import (
    AgentOutcome,
    AttributionStage,
    AttributionStatus,
    FailureCategory,
    InfraOutcome,
    LifecycleStatus,
    PatchKind,
    TaskValidationState,
)
from app.domain.enums import (
    TestRole as DomainTestRole,
)
from app.domain.enums import (
    TestStatus as DomainTestStatus,
)
from app.evaluation.orchestrator import create_runs
from app.infrastructure.models.attribution import FailureAttribution, HumanReview
from app.infrastructure.models.benchmark import BenchmarkTask
from app.infrastructure.models.evaluation import (
    EvaluationTaskRun,
    PatchArtifact,
)
from app.infrastructure.models.evaluation import (
    TestResult as ResultModel,
)
from tests.integration.factories import provenance_for, seed_minimal, wipe

pytestmark = pytest.mark.db


@dataclass(frozen=True, slots=True)
class ReviewWorld:
    task_run_ids: tuple[int, ...]


@pytest.fixture(autouse=True)
def clean(session: Session) -> None:
    wipe(session)


@pytest.fixture
def review_world(session: Session) -> ReviewWorld:
    seeded = seed_minimal(session, tasks=3)
    run = create_runs(
        session,
        name="review-fixture",
        task_ids=list(seeded.task_ids),
        provenance=provenance_for(seeded),
    )[0]
    task_run_ids: list[int] = []
    categories = [
        FailureCategory.F1_REQUIREMENT_MISUNDERSTANDING,
        FailureCategory.F3_INCOMPLETE_FIX,
        FailureCategory.F6_REGRESSION,
    ]
    for index, (task_id, category) in enumerate(
        zip(seeded.task_ids, categories, strict=True), start=1
    ):
        task = session.get(BenchmarkTask, task_id)
        assert task is not None
        task.raw_definition = {
            "gold_patch": ("--- a/src/x.py\n+++ b/src/x.py\n@@ -1 +1 @@\n-old\n+new\n")
        }
        task_run = EvaluationTaskRun(
            evaluation_run_id=run.id,
            benchmark_task_id=task_id,
            lifecycle_status=LifecycleStatus.COMPLETED,
            infra_outcome=InfraOutcome.SUCCESS,
            agent_outcome=AgentOutcome.UNRESOLVED,
            agent_started_at=run.created_at,
            is_canonical=True,
            f2p_passed=0,
            f2p_total=1,
            p2p_passed=1,
            p2p_total=1,
        )
        session.add(task_run)
        session.flush()
        task_run_ids.append(task_run.id)
        session.add_all(
            [
                FailureAttribution(
                    evaluation_task_run_id=task_run.id,
                    stage=(AttributionStage.LLM if index < 3 else AttributionStage.RULE),
                    category=category,
                    confidence=(0.8 if index < 3 else None),
                    judge_model=("fake/judge" if index < 3 else None),
                    prompt_hash=(str(index) * 64 if index < 3 else None),
                    evidence={"citations": [{"quote": f"证据 {index}"}]},
                    reasoning_zh=f"自动理由 {index}",
                    status=AttributionStatus.OK,
                ),
                PatchArtifact(
                    evaluation_task_run_id=task_run.id,
                    kind=PatchKind.AGENT_NORMALIZED,
                    uri=f"local://review/{index}.diff",
                    sha256=str(index) * 64,
                    size_bytes=20,
                    files_changed=1,
                    lines_added=1,
                    lines_deleted=1,
                    is_empty=False,
                    applies_cleanly=True,
                ),
                ResultModel(
                    evaluation_task_run_id=task_run.id,
                    test_id=f"tests/test_{index}.py::test_bug",
                    role=DomainTestRole.F2P,
                    status=DomainTestStatus.FAILED,
                    message_excerpt="AssertionError",
                ),
            ]
        )
    session.commit()
    return ReviewWorld(task_run_ids=tuple(task_run_ids))


def _headers(token: str) -> dict[str, str]:
    return {"X-Bench-Token": token}


def _queue(
    client: TestClient, token: str, *, reviewer: str = "alice", batch_id: str | None = None
) -> dict[str, Any]:
    params: dict[str, Any] = {"reviewer": reviewer, "seed": 7}
    if batch_id is not None:
        params["batch_id"] = batch_id
    response = client.get("/api/review/queue", params=params, headers=_headers(token))
    assert response.status_code == 200, response.text
    return response.json()


def test_queue_is_reproducible_and_does_not_leak_the_stratification_category(
    client: TestClient, admin_token: str, review_world: ReviewWorld
) -> None:
    first = _queue(client, admin_token)
    second = _queue(client, admin_token, batch_id=first["batch"]["batch_id"])

    assert first == second
    assert first["batch"]["selected_count"] == 3
    assert first["batch"]["insufficient_pool"] is True
    assert {item["task_run_id"] for item in first["items"]} == set(review_world.task_run_ids)
    assert "category" not in first["items"][0]
    assert "confidence" not in first["items"][0]


def test_case_hides_automatic_attribution_until_this_reviewer_submits(
    client: TestClient, admin_token: str, review_world: ReviewWorld
) -> None:
    queue = _queue(client, admin_token)
    batch_id = queue["batch"]["batch_id"]
    task_run_id = review_world.task_run_ids[0]
    params = {"batch_id": batch_id, "reviewer": "alice"}

    blind = client.get(
        f"/api/review/{task_run_id}", params=params, headers=_headers(admin_token)
    ).json()

    assert "automatic_attribution" not in blind
    assert "own_selected_category" not in blind
    assert blind["gold_patch"] == {
        "available": True,
        "files": ["src/x.py"],
        "lines_added": 1,
        "lines_deleted": 1,
    }
    assert blind["tests"][0]["message_excerpt"] == "AssertionError"

    submitted = client.post(
        f"/api/review/{task_run_id}",
        headers=_headers(admin_token),
        json={
            "batch_id": batch_id,
            "reviewer": "alice",
            "category": "F1_REQUIREMENT_MISUNDERSTANDING",
        },
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["action"] == "ACCEPT"
    assert submitted.json()["automatic_attribution"]["category"] == (
        "F1_REQUIREMENT_MISUNDERSTANDING"
    )

    alice = client.get(
        f"/api/review/{task_run_id}", params=params, headers=_headers(admin_token)
    ).json()
    bob = client.get(
        f"/api/review/{task_run_id}",
        params={"batch_id": batch_id, "reviewer": "bob"},
        headers=_headers(admin_token),
    ).json()
    assert alice["automatic_attribution"]["reasoning_zh"] == "自动理由 1"
    assert "automatic_attribution" not in bob


def test_comment_does_not_complete_a_label_or_unlock_the_answer(
    client: TestClient, admin_token: str, review_world: ReviewWorld
) -> None:
    queue = _queue(client, admin_token)
    batch_id = queue["batch"]["batch_id"]
    task_run_id = review_world.task_run_ids[0]

    response = client.post(
        f"/api/review/{task_run_id}",
        headers=_headers(admin_token),
        json={"batch_id": batch_id, "reviewer": "alice", "comment": "先记一条线索"},
    )

    assert response.status_code == 200
    assert response.json()["action"] == "COMMENT"
    assert response.json()["progress"]["label_count"] == 0
    assert "automatic_attribution" not in response.json()


def test_disagreement_requires_a_third_reviewer_and_final_n2_quarantines_task(
    client: TestClient,
    admin_token: str,
    review_world: ReviewWorld,
    session: Session,
) -> None:
    queue = _queue(client, admin_token)
    batch_id = queue["batch"]["batch_id"]
    task_run_id = review_world.task_run_ids[0]

    for reviewer, category in [
        ("alice", "F1_REQUIREMENT_MISUNDERSTANDING"),
        ("bob", "F2_WRONG_FILE_LOCALIZATION"),
    ]:
        response = client.post(
            f"/api/review/{task_run_id}",
            headers=_headers(admin_token),
            json={"batch_id": batch_id, "reviewer": reviewer, "category": category},
        )
        assert response.status_code == 200, response.text
    assert response.json()["progress"]["phase"] == "ARBITRATION"

    carol_queue = _queue(client, admin_token, reviewer="carol", batch_id=batch_id)
    item = next(row for row in carol_queue["items"] if row["task_run_id"] == task_run_id)
    assert item["required_phase"] == "ARBITRATION"

    arbitration = client.post(
        f"/api/review/{task_run_id}",
        headers=_headers(admin_token),
        json={
            "batch_id": batch_id,
            "reviewer": "carol",
            "category": "N2_TASK_DEFECT",
            "comment": "官方 F2P 与题面要求矛盾",
        },
    )
    assert arbitration.status_code == 200, arbitration.text
    assert arbitration.json()["progress"]["phase"] == "COMPLETE"
    assert arbitration.json()["progress"]["final_category"] == "N2_TASK_DEFECT"
    assert arbitration.json()["task_quarantined"] is True

    task = session.scalar(
        sa.select(BenchmarkTask)
        .join(EvaluationTaskRun, EvaluationTaskRun.benchmark_task_id == BenchmarkTask.id)
        .where(EvaluationTaskRun.id == task_run_id)
    )
    assert task is not None
    session.refresh(task)
    assert task.validation_state is TaskValidationState.QUARANTINED
    assert task.quarantine is not None
    assert task.quarantine["sample_batch_id"] == batch_id


def test_marking_task_defect_requires_a_reason(
    client: TestClient, admin_token: str, review_world: ReviewWorld
) -> None:
    queue = _queue(client, admin_token)
    response = client.post(
        f"/api/review/{review_world.task_run_ids[0]}",
        headers=_headers(admin_token),
        json={
            "batch_id": queue["batch"]["batch_id"],
            "reviewer": "alice",
            "category": "N2_TASK_DEFECT",
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "TASK_DEFECT_REASON_REQUIRED"


def test_same_reviewer_cannot_supply_both_independent_labels(
    client: TestClient, admin_token: str, review_world: ReviewWorld
) -> None:
    queue = _queue(client, admin_token)
    batch_id = queue["batch"]["batch_id"]
    task_run_id = review_world.task_run_ids[0]
    body = {
        "batch_id": batch_id,
        "reviewer": "alice",
        "category": "F1_REQUIREMENT_MISUNDERSTANDING",
    }

    assert (
        client.post(
            f"/api/review/{task_run_id}", headers=_headers(admin_token), json=body
        ).status_code
        == 200
    )
    duplicate = client.post(f"/api/review/{task_run_id}", headers=_headers(admin_token), json=body)

    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "REVIEWER_ALREADY_SUBMITTED"
    assert session_count_reviews(client, admin_token, batch_id, task_run_id) == 1


def session_count_reviews(client: TestClient, token: str, batch_id: str, task_run_id: int) -> int:
    """通过队列行为确认重复提交没有新增标注；数据库条数在专门测试里覆盖。"""
    body = client.get(
        f"/api/review/{task_run_id}",
        params={"batch_id": batch_id, "reviewer": "alice"},
        headers=_headers(token),
    ).json()
    return body["progress"]["label_count"]


def test_review_rows_are_saved_with_batch_blind_flag_and_derived_action(
    client: TestClient,
    admin_token: str,
    review_world: ReviewWorld,
    session: Session,
) -> None:
    queue = _queue(client, admin_token)
    batch_id = queue["batch"]["batch_id"]
    task_run_id = review_world.task_run_ids[0]

    response = client.post(
        f"/api/review/{task_run_id}",
        headers=_headers(admin_token),
        json={
            "batch_id": batch_id,
            "reviewer": "alice",
            "category": "F4_INCORRECT_LOGIC",
            "comment": "改动位置对，但条件写反了",
        },
    )
    assert response.status_code == 200

    row = session.scalar(sa.select(HumanReview))
    assert row is not None
    session.refresh(row)
    assert row.sample_batch_id == batch_id
    assert row.blind is True
    assert row.action.value == "CORRECT"
    assert row.corrected_category is FailureCategory.F4_INCORRECT_LOGIC


def test_review_endpoints_require_admin_token(
    client: TestClient, review_world: ReviewWorld
) -> None:
    response = client.get("/api/review/queue", params={"reviewer": "alice"})

    assert response.status_code == 403
    assert response.json()["code"] == "MISSING_TOKEN"


def test_automatic_attribution_is_frozen_after_the_first_human_label(
    client: TestClient,
    admin_token: str,
    review_world: ReviewWorld,
    session: Session,
) -> None:
    queue = _queue(client, admin_token)
    batch_id = queue["batch"]["batch_id"]
    task_run_id = review_world.task_run_ids[2]
    submitted = client.post(
        f"/api/review/{task_run_id}",
        headers=_headers(admin_token),
        json={
            "batch_id": batch_id,
            "reviewer": "alice",
            "category": "F6_REGRESSION",
        },
    )
    assert submitted.status_code == 200

    report = save_rule_verdicts(
        session,
        [
            (
                task_run_id,
                RuleVerdict(
                    category=FailureCategory.F7_EMPTY_OR_INVALID_PATCH,
                    rule=RuleName.EMPTY_OR_INVALID_PATCH,
                    evidence={"agent_outcome": "EMPTY_PATCH"},
                ),
            )
        ],
    )
    session.flush()

    attribution = session.scalar(
        sa.select(FailureAttribution).where(
            FailureAttribution.evaluation_task_run_id == task_run_id
        )
    )
    assert report.protected == 1
    assert attribution is not None
    assert attribution.category is FailureCategory.F6_REGRESSION


def test_metrics_reports_accuracy_kappa_and_confusion_matrix(
    client: TestClient, admin_token: str, review_world: ReviewWorld
) -> None:
    queue = _queue(client, admin_token)
    batch_id = queue["batch"]["batch_id"]
    t0, t1, _t2 = review_world.task_run_ids

    # t0 的自动类别是 F1，两个标注者都接受——一对双人标注、结论一致
    for reviewer in ("alice", "bob"):
        response = client.post(
            f"/api/review/{t0}",
            headers=_headers(admin_token),
            json={
                "batch_id": batch_id,
                "reviewer": reviewer,
                "category": "F1_REQUIREMENT_MISUNDERSTANDING",
            },
        )
        assert response.status_code == 200, response.text

    # t1 的自动类别是 F3，人工改判成 F4——只有一人标注，算不进 κ 但算准确率
    response = client.post(
        f"/api/review/{t1}",
        headers=_headers(admin_token),
        json={"batch_id": batch_id, "reviewer": "alice", "category": "F4_INCORRECT_LOGIC"},
    )
    assert response.status_code == 200, response.text

    metrics = client.get("/api/review/metrics", headers=_headers(admin_token))
    assert metrics.status_code == 200, metrics.text
    body = metrics.json()

    assert body["sample_count"] == 3
    assert body["accuracy"] == {"available": True, "reason": None}
    assert body["accuracy_value"] == pytest.approx(2 / 3)
    assert body["kappa"] == {"available": True, "reason": None}
    assert body["kappa_value"] == pytest.approx(1.0)
    confusion = {
        (cell["automatic_category"], cell["human_category"]): cell["count"]
        for cell in body["confusion_matrix"]
    }
    assert confusion == {
        ("F1_REQUIREMENT_MISUNDERSTANDING", "F1_REQUIREMENT_MISUNDERSTANDING"): 2,
        ("F3_INCOMPLETE_FIX", "F4_INCORRECT_LOGIC"): 1,
    }


def test_metrics_is_unavailable_without_any_labelled_review_and_requires_admin_token(
    client: TestClient, admin_token: str, review_world: ReviewWorld
) -> None:
    metrics = client.get("/api/review/metrics", headers=_headers(admin_token))
    assert metrics.status_code == 200, metrics.text
    body = metrics.json()

    assert body["sample_count"] == 0
    assert body["accuracy"]["available"] is False
    assert body["accuracy_value"] is None
    assert body["kappa"]["available"] is False
    assert body["kappa_value"] is None
    assert body["confusion_matrix"] == []

    unauthorized = client.get("/api/review/metrics")
    assert unauthorized.status_code == 403
    assert unauthorized.json()["code"] == "MISSING_TOKEN"


def test_metrics_batch_id_filter_excludes_reviews_from_other_batches(
    client: TestClient, admin_token: str, review_world: ReviewWorld
) -> None:
    queue = _queue(client, admin_token)
    batch_id = queue["batch"]["batch_id"]
    client.post(
        f"/api/review/{review_world.task_run_ids[0]}",
        headers=_headers(admin_token),
        json={
            "batch_id": batch_id,
            "reviewer": "alice",
            "category": "F1_REQUIREMENT_MISUNDERSTANDING",
        },
    )

    scoped = client.get(
        "/api/review/metrics",
        params={"batch_id": "review-v1-s0-a0-n1-d" + "0" * 32},
        headers=_headers(admin_token),
    )
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["sample_count"] == 0

    matching = client.get(
        "/api/review/metrics", params={"batch_id": batch_id}, headers=_headers(admin_token)
    )
    assert matching.status_code == 200, matching.text
    assert matching.json()["sample_count"] == 1
