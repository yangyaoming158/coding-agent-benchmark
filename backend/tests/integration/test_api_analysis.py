"""失败分析接口（E7-T6）。

口径复用报告的 `_failures()`，这里只验 API 层自己负责的三件事：
看哪几次实验（数据集 → 排行榜准入；实验号 → 原样）、盲检期间藏逐案例答案、
SQL 条数不随实验数增长。分布算得对不对由 `test_report_generation.py` 管。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.domain.enums import (
    AgentKind,
    AgentOutcome,
    AttributionStage,
    AttributionStatus,
    FailureCategory,
)
from app.infrastructure.models.attribution import FailureAttribution
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun
from tests.integration.conftest import count_queries
from tests.integration.test_api_leaderboard import World, clean  # noqa: F401  # autouse 夹具

pytestmark = pytest.mark.db


@pytest.fixture
def world(session: Session) -> World:
    return World(session)


def _attribute(
    session: Session,
    run: EvaluationRun,
    *,
    category: FailureCategory,
    stage: AttributionStage = AttributionStage.RULE,
    status: AttributionStatus = AttributionStatus.OK,
    how_many: int = 1,
) -> None:
    """给这次实验里前 how_many 条没解决的执行各写一条归因。"""
    task_runs = session.query(EvaluationTaskRun).filter_by(evaluation_run_id=run.id).all()
    failed = [t for t in task_runs if t.agent_outcome is not AgentOutcome.RESOLVED]
    for task_run in failed[:how_many]:
        session.add(
            FailureAttribution(
                evaluation_task_run_id=task_run.id,
                stage=stage,
                category=category,
                evidence={"rule": "regression", "facts": {}},
                status=status,
                reasoning_zh="测试" if stage is AttributionStage.LLM else None,
            )
        )
    session.flush()


def test_by_dataset_uses_leaderboard_eligibility(
    client: TestClient, session: Session, world: World
) -> None:
    """按数据集查时，只算排行榜准入的实验：dirty 的、被排除的、哨兵的失败不混进来。"""
    good = world.contestant("a", "a@m", kind=AgentKind.CLI)
    eligible = world.run(good, resolved=4)  # 2 道失败
    world.run(good, resolved=0, dirty=True)  # 6 道失败，dirty，不准入
    world.run(good, resolved=0, excluded="判定漏洞")  # 不准入
    oracle = world.contestant("oracle", "oracle@gold", kind=AgentKind.ORACLE)
    world.run(oracle, resolved=6)
    _attribute(session, eligible, category=FailureCategory.F6_REGRESSION, how_many=1)
    session.commit()

    body = client.get("/api/analysis", params={"set": "benchmark-dev", "version": "v1"}).json()

    assert body["benchmark_set"] == "benchmark-dev@v1"
    assert body["run_ids"] == [eligible.id]
    failures = body["failures"]
    assert failures["total_failures"] == 2
    assert failures["attributed_failures"] == 1
    assert failures["unattributed_failures"] == 1
    assert failures["category_counts"] == {"F6_REGRESSION": 1}
    assert failures["heatmap"] == [{"agent_label": "a@m", "category": "F6_REGRESSION", "count": 1}]
    assert [case["run_id"] for case in failures["top_cases"]] == [eligible.id, eligible.id]
    assert body["attribution_withheld"] is False


def test_by_run_ids_takes_them_as_given_and_404s_on_unknown(
    client: TestClient, session: Session, world: World
) -> None:
    """按实验号查时不做准入过滤（就是为了看被排除的那些）；不存在的号直接 404，不静默跳过。"""
    good = world.contestant("a", "a@m", kind=AgentKind.CLI)
    excluded = world.run(good, resolved=3, excluded="对照组")
    session.commit()

    body = client.get("/api/analysis", params={"run": [excluded.id]}).json()
    assert body["benchmark_set"] is None
    assert body["run_ids"] == [excluded.id]
    assert body["failures"]["total_failures"] == 3

    missing = client.get("/api/analysis", params={"run": [excluded.id, 999_999]})
    assert missing.status_code == 404
    assert missing.json()["code"] == "RUN_NOT_FOUND"


def test_scope_must_be_given_and_not_both(
    client: TestClient, session: Session, world: World
) -> None:
    """set 和 run 二选一：都不给或都给都是 422，错误码稳定。"""
    assert client.get("/api/analysis").json()["code"] == "ANALYSIS_SCOPE_MISSING"
    both = client.get("/api/analysis", params={"set": "benchmark-dev", "run": [1]})
    assert both.status_code == 422
    assert both.json()["code"] == "ANALYSIS_SCOPE_AMBIGUOUS"


def test_needs_human_and_llm_rows_are_counted_separately(
    client: TestClient, session: Session, world: World
) -> None:
    """LLM 层的结论数和 NEEDS_HUMAN 数单独给出；它们仍在 category_counts 里，页面要能单独标。"""
    good = world.contestant("a", "a@m", kind=AgentKind.CLI)
    run = world.run(good, resolved=3)  # 3 道失败
    _attribute(session, run, category=FailureCategory.F6_REGRESSION, how_many=1)
    task_runs = session.query(EvaluationTaskRun).filter_by(evaluation_run_id=run.id).all()
    failed = [t for t in task_runs if t.agent_outcome is not AgentOutcome.RESOLVED]
    session.add(
        FailureAttribution(
            evaluation_task_run_id=failed[1].id,
            stage=AttributionStage.LLM,
            category=FailureCategory.F4_INCORRECT_LOGIC,
            evidence={"citations": [], "vote_categories": []},
            status=AttributionStatus.NEEDS_HUMAN,
            reasoning_zh="三票不一致",
        )
    )
    session.commit()

    failures = client.get("/api/analysis", params={"run": [run.id]}).json()["failures"]

    assert failures["attributed_failures"] == 2
    assert failures["llm_attributed_failures"] == 1
    assert failures["needs_human_failures"] == 1
    assert failures["category_counts"] == {"F4_INCORRECT_LOGIC": 1, "F6_REGRESSION": 1}
    statuses = {case["task_run_id"]: case["attribution_status"] for case in failures["top_cases"]}
    assert statuses[failed[1].id] == "NEEDS_HUMAN"
    assert failures["llm_attribution"]["available"] is True


def test_blind_review_withholds_per_case_answers_but_keeps_the_distribution(
    client: TestClient, session: Session, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """盲检开着时逐案例的类别 / 状态 / 理由置空，分布和热力图照常（从总数猜不出单条）。"""
    from app.infrastructure.config import reset_settings_cache

    good = world.contestant("a", "a@m", kind=AgentKind.CLI)
    run = world.run(good, resolved=4)
    _attribute(session, run, category=FailureCategory.F6_REGRESSION, how_many=2)
    session.commit()

    monkeypatch.setenv("BENCH_BLIND_REVIEW", "true")
    reset_settings_cache()
    try:
        body = client.get("/api/analysis", params={"run": [run.id]}).json()
    finally:
        monkeypatch.delenv("BENCH_BLIND_REVIEW")
        reset_settings_cache()

    assert body["attribution_withheld"] is True
    assert body["failures"]["category_counts"] == {"F6_REGRESSION": 2}
    for case in body["failures"]["top_cases"]:
        assert case["category"] is None
        assert case["attribution_status"] is None
        assert case["reasoning_zh"] is None


def test_query_count_does_not_grow_with_runs_or_failures(
    client: TestClient, session: Session, world: World, engine: Engine
) -> None:
    """SQL 条数不随实验数、失败数增长（AC-7）。"""
    good = world.contestant("a", "a@m", kind=AgentKind.CLI)
    run = world.run(good, resolved=5)
    _attribute(session, run, category=FailureCategory.F6_REGRESSION)
    session.commit()
    with count_queries(engine) as few:
        assert client.get("/api/analysis", params={"set": "benchmark-dev"}).status_code == 200

    for name in ("b", "c", "d"):
        other = world.run(world.contestant(name, f"{name}@m", kind=AgentKind.CLI), resolved=0)
        _attribute(session, other, category=FailureCategory.F7_EMPTY_OR_INVALID_PATCH, how_many=6)
    session.commit()
    with count_queries(engine) as many:
        body = client.get("/api/analysis", params={"set": "benchmark-dev"}).json()

    assert body["failures"]["total_failures"] == 1 + 18
    assert few.count == many.count, many.statements
