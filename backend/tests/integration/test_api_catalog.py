"""目录类接口（E7-T0）：数据集版本、题目、Agent 与参赛者。

这三组端点是 §16.2 里 Benchmarks / Benchmark Detail / Agents 三个页面的数据源。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.domain.enums import (
    AgentKind,
    BenchmarkSetStatus,
    IssueLanguage,
    TaskDifficulty,
    TaskValidationState,
)
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.benchmark import (
    BenchmarkSet,
    BenchmarkSetItem,
    BenchmarkTask,
    EnvironmentSpec,
    Repository,
)
from tests.integration.conftest import count_queries
from tests.integration.factories import seed_minimal, wipe

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def clean(session: Session) -> None:
    wipe(session)


@pytest.fixture
def catalog(session: Session) -> dict[str, Any]:
    """两个仓库、两种语言、三种难度的一小批题，冻进一个已发布的快照。

    刻意做得有区分度：Benchmark Detail 页的四个筛选器要有东西可筛，
    构成统计也要能看出分布，全用同一个取值的数据测不出这两件事。
    """
    repo_a = Repository(full_name="pallets/click", url="https://x", language="python")
    repo_b = Repository(full_name="bench/demo", url="https://y", language="python")
    session.add_all([repo_a, repo_b])
    session.flush()

    env = EnvironmentSpec(
        environment_id="pallets__click__py311",
        repository_id=repo_a.id,
        python_version="3.11",
        install_command="pip install -e .",
        test_command="pytest",
        test_report_path="report/junit.xml",
    )
    dataset = BenchmarkSet(
        slug="benchmark-dev",
        version="v1",
        title="开发集",
        status=BenchmarkSetStatus.PUBLISHED,
        task_count=3,
        source_dataset_id="benchmark-dev",
        snapshot_digest="a" * 64,
        publish_evidence={"oracle_run_id": 1, "noop_run_id": 2},
    )
    session.add_all([env, dataset])
    session.flush()

    specs = [
        ("pallets__click-1", repo_a, IssueLanguage.ZH, TaskDifficulty.EASY),
        ("pallets__click-2", repo_a, IssueLanguage.EN, TaskDifficulty.HARD),
        ("bench__demo-1", repo_b, IssueLanguage.ZH, TaskDifficulty.MEDIUM),
    ]
    task_ids: list[int] = []
    for position, (task_id, repo, language, difficulty) in enumerate(specs):
        task = BenchmarkTask(
            task_id=task_id,
            repository_id=repo.id,
            environment_spec_id=env.id,
            base_commit="a" * 40,
            issue_title=f"{task_id} 的标题",
            issue_body="正文很长",
            issue_language=language,
            fail_to_pass=["tests/test_a.py::test_new", "tests/test_a.py::test_other"],
            pass_to_pass=["tests/test_b.py::test_old"],
            test_patch_uri="local://test.patch",
            test_patch_paths=["tests/test_a.py"],
            gold_patch_uri="local://gold.patch",
            difficulty=difficulty,
            validation_state=TaskValidationState.VALID,
            content_hash=f"{position:064d}",
            raw_definition={},
        )
        session.add(task)
        session.flush()
        task_ids.append(task.id)
        session.add(
            BenchmarkSetItem(
                benchmark_set_id=dataset.id,
                benchmark_task_id=task.id,
                task_content_hash=task.content_hash,
                position=position,
            )
        )
    session.commit()
    return {"dataset_id": dataset.id, "task_ids": task_ids}


# ── 数据集版本 ──────────────────────────────────────────────


def test_benchmark_set_list(client: TestClient, catalog: dict[str, Any]) -> None:
    body = client.get("/api/benchmark-sets").json()

    assert body["total"] == 1
    row = body["items"][0]
    assert row["slug"] == "benchmark-dev"
    # 枚举原样透出，不做中文映射（AC-10）
    assert row["status"] == "PUBLISHED"
    assert row["task_count"] == 3
    assert row["snapshot_digest"] == "a" * 64


def test_benchmark_set_detail_has_composition_and_publish_evidence(
    client: TestClient, catalog: dict[str, Any]
) -> None:
    """详情要能回答 Benchmarks 页的"语言分布/来源构成"和 Detail 页的"自检结果"。"""
    body = client.get("/api/benchmark-sets/benchmark-dev").json()

    assert body["publish_evidence"] == {"oracle_run_id": 1, "noop_run_id": 2}
    composition = body["composition"]
    assert composition["language"] == [
        {"value": "en", "count": 1},
        {"value": "zh", "count": 2},
    ]
    assert composition["difficulty"] == [
        {"value": "easy", "count": 1},
        {"value": "hard", "count": 1},
        {"value": "medium", "count": 1},
    ]
    assert composition["repository"] == [
        {"value": "bench/demo", "count": 1},
        {"value": "pallets/click", "count": 2},
    ]


def test_unknown_benchmark_set_is_404(client: TestClient) -> None:
    response = client.get("/api/benchmark-sets/nope")

    assert response.status_code == 404
    assert response.json()["code"] == "BENCHMARK_SET_NOT_FOUND"


# ── 题目 ────────────────────────────────────────────────────


def test_task_list_filters_match_the_benchmark_detail_page(
    client: TestClient, catalog: dict[str, Any]
) -> None:
    """四个筛选器加搜索框，都对得上 §16.2 的 Benchmark Detail 页。

    §14.4 原本只写了 `set/state/q` 三个参数，仓库/难度/语言是按那一页倒推补的。
    """
    base = {"set": catalog["dataset_id"]}

    assert client.get("/api/tasks", params=base).json()["total"] == 3
    assert client.get("/api/tasks", params={**base, "repo": "pallets/click"}).json()["total"] == 2
    assert client.get("/api/tasks", params={**base, "difficulty": "hard"}).json()["total"] == 1
    assert client.get("/api/tasks", params={**base, "language": "zh"}).json()["total"] == 2
    assert client.get("/api/tasks", params={**base, "state": "VALID"}).json()["total"] == 3
    assert client.get("/api/tasks", params={**base, "q": "demo"}).json()["total"] == 1


def test_task_list_query_count_does_not_grow_with_rows(
    client: TestClient, session: Session, engine: Engine, catalog: dict[str, Any]
) -> None:
    """题目列表里的仓库名和环境 ID 是 join 出来的，不是逐行再查（AC-7）。"""
    with count_queries(engine) as few:
        assert client.get("/api/tasks", params={"limit": 1}).status_code == 200
    with count_queries(engine) as many:
        assert client.get("/api/tasks", params={"limit": 50}).json()["total"] == 3

    assert few.count == many.count <= 2, many.statements


def test_task_detail_gives_the_full_issue_and_case_lists(
    client: TestClient, catalog: dict[str, Any]
) -> None:
    body = client.get("/api/tasks/pallets__click-1").json()

    assert body["issue_body"] == "正文很长"
    assert body["fail_to_pass"] == ["tests/test_a.py::test_new", "tests/test_a.py::test_other"]
    assert body["pass_to_pass"] == ["tests/test_b.py::test_old"]
    assert body["repository"] == "pallets/click"
    assert body["environment_id"] == "pallets__click__py311"
    assert body["fail_to_pass_count"] == 2
    assert body["agent_timeout_s"] == 720


def test_task_endpoints_never_leak_the_gold_patch(
    client: TestClient, catalog: dict[str, Any]
) -> None:
    """`gold_patch_uri` 和 `test_patch_paths` 一个都不透出。

    协议 C-44 禁止把官方修复补丁发给被测 AI，C-76 禁止下发 `test_patch_paths`。
    读接口是开放的（§14.4：写操作要 token，读接口开放），任何人都能拉 ——
    放进来等于给绕过任务输入开了第二扇门。
    """
    detail = client.get("/api/tasks/pallets__click-1").json()
    listed = client.get("/api/tasks").json()["items"][0]

    for body in (detail, listed):
        assert "gold_patch_uri" not in body
        assert "test_patch_paths" not in body


def test_unknown_task_is_404(client: TestClient) -> None:
    response = client.get("/api/tasks/nope__nope-1")

    assert response.status_code == 404
    assert response.json() == {"code": "TASK_NOT_FOUND", "message": "找不到题目 nope__nope-1"}


# ── Agent 与参赛者 ──────────────────────────────────────────


def test_agents_and_configs_are_separate_resources(client: TestClient, session: Session) -> None:
    """Agent 是适配器定义，AgentConfig 才是排行榜上的参赛者。

    同一个 aider 接两个模型就是两个参赛者，适配器代码只有一份 ——
    合成一个端点的话，这件事在接口上就看不见了。
    """
    agent = Agent(
        name="aider", display_name="Aider", kind=AgentKind.CLI, adapter_class="AiderRunner"
    )
    session.add(agent)
    session.flush()
    session.add_all(
        [
            AgentConfig(
                agent_id=agent.id,
                label="aider@deepseek-chat",
                agent_version="0.86",
                model_name="deepseek/deepseek-chat",
                config_hash="a" * 64,
            ),
            AgentConfig(
                agent_id=agent.id,
                label="aider@deepseek-chat+autotest",
                agent_version="0.86",
                model_name="deepseek/deepseek-chat",
                config_hash="b" * 64,
                enabled=False,
            ),
        ]
    )
    session.commit()

    agents = client.get("/api/agents").json()
    configs = client.get("/api/agent-configs").json()

    assert agents["total"] == 1
    assert agents["items"][0]["kind"] == "CLI"
    assert configs["total"] == 2
    # 一条 SQL 就带出了所属 Agent 的名字，前端不用为每行再发一次请求
    assert {c["agent_name"] for c in configs["items"]} == {"aider"}
    assert [c["enabled"] for c in configs["items"]] == [True, False]


def test_agent_configs_can_filter_by_enabled(client: TestClient, session: Session) -> None:
    """按启用状态筛。库里的诊断参赛者是停用的，Agents 页要能分开看。"""
    agent = Agent(name="a", display_name="A", kind=AgentKind.CLI, adapter_class="X")
    session.add(agent)
    session.flush()
    session.add_all(
        [
            AgentConfig(
                agent_id=agent.id,
                label="on",
                agent_version="1",
                model_name="m",
                config_hash="a" * 64,
            ),
            AgentConfig(
                agent_id=agent.id,
                label="off",
                agent_version="1",
                model_name="m",
                config_hash="b" * 64,
                enabled=False,
            ),
        ]
    )
    session.commit()

    assert client.get("/api/agent-configs", params={"enabled": True}).json()["total"] == 1
    assert client.get("/api/agent-configs", params={"enabled": False}).json()["total"] == 1


def test_agent_config_price_is_null_not_zero_when_unset(
    client: TestClient, session: Session
) -> None:
    """没配单价时是 null，不是 0 —— 0 会被读成"免费"。"""
    seeded = seed_minimal(session)
    session.commit()

    item = client.get("/api/agent-configs").json()["items"][0]

    assert item["id"] == seeded.agent_config_id
    assert item["price_input_per_mtok"] is None
    assert item["price_output_per_mtok"] is None
