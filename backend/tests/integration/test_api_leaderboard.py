"""排行榜的准入口径（E7-T0）。

数据照着库里真实的样子造：一个 22 题的已发布快照，上面跑过哨兵、探测跑、
诊断参赛者、被排除的实验和两个真参赛者。每条测试验一条资格规则。

**为什么这些规则值得逐条钉住**：协议只给了两条（C-26 的 5% 门槛、C-28 的 dirty），
按那两条筛完，库里 18 个实验有 14 个"合格"，其中包括 4 个一次模型都没调到的
（细账见 `07-platform-architecture.md` §18.6 第九节）。少一条规则，
排行榜就会安安静静地给出一个错两倍的数字。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.domain.enums import (
    AgentKind,
    AgentOutcome,
    BenchmarkSetStatus,
    CostSource,
    EvaluationRunStatus,
    InfraOutcome,
    IssueLanguage,
    LifecycleStatus,
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
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun
from tests.integration.conftest import count_queries
from tests.integration.factories import wipe

pytestmark = pytest.mark.db

#: 快照里的题数。和库里的 benchmark-dev@v1 一样，方便对照着读。
SNAPSHOT_TASKS = 6


@pytest.fixture(autouse=True)
def clean(session: Session) -> None:
    wipe(session)


class World:
    """一小套完整的库存：数据集、题、参赛者，外加建实验的快捷方法。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        repo = Repository(full_name="pallets/click", url="https://x", language="python")
        session.add(repo)
        session.flush()
        env = EnvironmentSpec(
            environment_id="pallets__click__py311",
            repository_id=repo.id,
            python_version="3.11",
            install_command="pip install -e .",
            test_command="pytest",
            test_report_path="report/junit.xml",
        )
        self.dataset = BenchmarkSet(
            slug="benchmark-dev",
            version="v1",
            title="开发集",
            status=BenchmarkSetStatus.PUBLISHED,
            task_count=SNAPSHOT_TASKS,
        )
        session.add_all([env, self.dataset])
        session.flush()

        self.task_ids: list[int] = []
        for index in range(SNAPSHOT_TASKS):
            task = BenchmarkTask(
                task_id=f"pallets__click-{index}",
                repository_id=repo.id,
                environment_spec_id=env.id,
                base_commit="a" * 40,
                issue_title=f"标题 {index}",
                issue_body="正文",
                issue_language=IssueLanguage.ZH if index % 2 else IssueLanguage.EN,
                fail_to_pass=["tests/test_a.py::test_new"],
                pass_to_pass=[],
                test_patch_uri="local://test.patch",
                test_patch_paths=["tests/test_a.py"],
                gold_patch_uri="local://gold.patch",
                difficulty=TaskDifficulty.EASY if index < 3 else TaskDifficulty.HARD,
                validation_state=TaskValidationState.VALID,
                content_hash=f"{index:064d}",
                raw_definition={},
            )
            session.add(task)
            session.flush()
            self.task_ids.append(task.id)
            session.add(
                BenchmarkSetItem(
                    benchmark_set_id=self.dataset.id,
                    benchmark_task_id=task.id,
                    task_content_hash=task.content_hash,
                    position=index,
                )
            )
        session.flush()

    def contestant(self, name: str, label: str, *, kind: AgentKind, enabled: bool = True) -> int:
        agent = Agent(
            name=name, display_name=name.title(), kind=kind, adapter_class=f"{name}Runner"
        )
        self.session.add(agent)
        self.session.flush()
        config = AgentConfig(
            agent_id=agent.id,
            label=label,
            agent_version="1.0",
            model_name="deepseek/deepseek-chat",
            config_hash=f"{agent.id:064d}",
            enabled=enabled,
        )
        self.session.add(config)
        self.session.flush()
        return int(config.id)

    def run(
        self,
        config_id: int,
        *,
        resolved: int,
        tasks: int = SNAPSHOT_TASKS,
        status: EvaluationRunStatus = EvaluationRunStatus.COMPLETED,
        dirty: bool = False,
        excluded: str | None = None,
        protocol: str = "v1.2",
        cost: str = "0.30",
        cost_source: CostSource = CostSource.REPORTED,
        with_task_runs: bool = True,
    ) -> EvaluationRun:
        run = EvaluationRun(
            name="r",
            benchmark_set_id=self.dataset.id,
            agent_config_id=config_id,
            status=status,
            total_tasks=tasks,
            completed_tasks=tasks,
            resolved_count=resolved,
            infra_failure_count=0,
            strict_resolve_rate=Decimal(resolved) / Decimal(tasks),
            effective_resolve_rate=Decimal(resolved) / Decimal(tasks),
            total_cost_usd=Decimal(cost),
            total_tokens=1_000_000,
            makespan_ms=600_000,
            protocol_version=protocol,
            dirty=dirty,
            leaderboard_excluded_reason=excluded,
            manifest={},
        )
        self.session.add(run)
        self.session.flush()
        if with_task_runs:
            for index in range(tasks):
                self.session.add(
                    EvaluationTaskRun(
                        evaluation_run_id=run.id,
                        benchmark_task_id=self.task_ids[index],
                        lifecycle_status=LifecycleStatus.COMPLETED,
                        infra_outcome=InfraOutcome.SUCCESS,
                        agent_outcome=(
                            AgentOutcome.RESOLVED if index < resolved else AgentOutcome.UNRESOLVED
                        ),
                        agent_started_at=run.created_at,
                        is_canonical=True,
                        cost_source=cost_source,
                    )
                )
        self.session.flush()
        return run


@pytest.fixture
def world(session: Session) -> World:
    return World(session)


def labels(body: dict[str, Any]) -> list[str]:
    return [row["label"] for row in body["rows"]["items"]]


# ── 六条资格 ────────────────────────────────────────────────


def test_a_normal_contestant_shows_up(client: TestClient, session: Session, world: World) -> None:
    config = world.contestant("aider", "aider@deepseek-chat", kind=AgentKind.CLI)
    world.run(config, resolved=2)
    session.commit()

    body = client.get("/api/leaderboard").json()

    assert labels(body) == ["aider@deepseek-chat"]
    assert body["benchmark_set"] == "benchmark-dev@v1"
    assert body["rows"]["items"][0]["rank"] == 1


def test_partial_runs_are_out(client: TestClient, session: Session, world: World) -> None:
    """`PARTIAL` 是"降级"，按 C-26b 不准入。"""
    config = world.contestant("aider", "aider@deepseek-chat", kind=AgentKind.CLI)
    world.run(config, resolved=2, status=EvaluationRunStatus.PARTIAL)
    session.commit()

    assert labels(client.get("/api/leaderboard").json()) == []


def test_dirty_runs_are_out(client: TestClient, session: Session, world: World) -> None:
    """协议 C-28：工作区不干净时跑出来的结果不得进排行榜。"""
    config = world.contestant("aider", "aider@deepseek-chat", kind=AgentKind.CLI)
    world.run(config, resolved=2, dirty=True)
    session.commit()

    assert labels(client.get("/api/leaderboard").json()) == []


def test_manually_excluded_runs_are_out_and_the_reason_is_shown(
    client: TestClient, session: Session, world: World
) -> None:
    """人工排除的实验不上榜，但**理由要出现在响应里**。

    这一条管的是库里 #119–#122 那种情况：`COMPLETED / 零平台故障 /
    dirty=false`，按协议完全合格，数字却不是测量结果（余额耗尽，一次模型都没调到）。
    不说出来的话，榜单看起来只是"aider 考了 0 分"。
    """
    config = world.contestant("aider", "aider@deepseek-chat", kind=AgentKind.CLI)
    run = world.run(config, resolved=0, excluded="余额耗尽，一次模型都没调到")
    session.commit()

    body = client.get("/api/leaderboard").json()

    assert labels(body) == []
    assert body["excluded_runs"] == [
        {"evaluation_run_id": run.id, "reason": "余额耗尽，一次模型都没调到"}
    ]


def test_disabled_contestants_are_out(client: TestClient, session: Session, world: World) -> None:
    """停用的参赛者不上榜。

    库里的 `aider@deepseek-chat+autotest` 是一次性诊断，不是选手，
    而且它**故意不在 `cli/seed.py` 里** —— 靠 `enabled` 这一列把它挡住。
    """
    config = world.contestant(
        "aider-autotest", "aider@deepseek-chat+autotest", kind=AgentKind.CLI, enabled=False
    )
    world.run(config, resolved=4)
    session.commit()

    assert labels(client.get("/api/leaderboard").json()) == []


@pytest.mark.parametrize("kind", [AgentKind.ORACLE, AgentKind.NOOP, AgentKind.MOCK])
def test_sentinels_are_out(
    client: TestClient, session: Session, world: World, kind: AgentKind
) -> None:
    """哨兵是量具不是选手。

    Oracle 永远 100%、Noop 永远 0%，让它们上榜只会把榜首和榜尾各占一格，
    而且会让人以为"有个参赛者满分"。
    """
    config = world.contestant(kind.value.lower(), f"{kind.value.lower()}@x", kind=kind)
    world.run(config, resolved=SNAPSHOT_TASKS if kind is AgentKind.ORACLE else 0)
    session.commit()

    assert labels(client.get("/api/leaderboard").json()) == []


def test_partial_coverage_runs_are_out(client: TestClient, session: Session, world: World) -> None:
    """只跑了快照一部分的实验不上榜。

    严格解决率的分母是题库总题数（协议 C-21）。库里的 #117 / #118 只跑了 2 道题、
    #124 只跑了 1 道 —— 它们的 0% 和跑满 22 道的 0% 根本不是一个数，
    放进同一列排名就是在比两个不同的东西。
    """
    config = world.contestant("aider", "aider@deepseek-chat", kind=AgentKind.CLI)
    world.run(config, resolved=0, tasks=2)
    session.commit()

    assert labels(client.get("/api/leaderboard").json()) == []


# ── 分组与展示 ──────────────────────────────────────────────


def test_rounds_collapse_and_ranking_reflects_real_pilot_numbers(
    client: TestClient, session: Session, world: World
) -> None:
    """两个真参赛者各跑两轮，合成两行，按平均解决率排名。

    数字按 E9-T1 pilot 的形状造：claude-code 两轮都高，aider 两轮有明显抖动。
    """
    aider = world.contestant("aider", "aider@deepseek-chat", kind=AgentKind.CLI)
    claude = world.contestant("claude-code", "claude-code@deepseek-chat", kind=AgentKind.CLI)
    world.run(aider, resolved=1)
    world.run(aider, resolved=3)
    world.run(claude, resolved=5)
    world.run(claude, resolved=5)
    session.commit()

    rows = client.get("/api/leaderboard").json()["rows"]["items"]

    assert [r["label"] for r in rows] == [
        "claude-code@deepseek-chat",
        "aider@deepseek-chat",
    ]
    assert [r["rank"] for r in rows] == [1, 2]
    assert rows[0]["run_count"] == 2
    # 两轮一样 → 抖动为 0；aider 两轮差两道题 → 抖动 2/6
    assert Decimal(rows[0]["resolve_rate_spread"]) == Decimal("0.0000")
    assert Decimal(rows[1]["resolve_rate_spread"]) == Decimal("0.3333")
    assert rows[1]["run_ids"] == sorted(rows[1]["run_ids"])


def test_different_protocol_versions_do_not_get_mixed(
    client: TestClient, session: Session, world: World
) -> None:
    """协议 C-59：不同协议版本的结果要分开展示，不能混排。

    门槛是评判标准的一部分，把两个门槛下的成绩排在一起就没法解释名次。
    """
    config = world.contestant("aider", "aider@deepseek-chat", kind=AgentKind.CLI)
    world.run(config, resolved=1, protocol="v1.2")
    world.run(config, resolved=5, protocol="v1.3")
    session.commit()

    rows = client.get("/api/leaderboard").json()["rows"]["items"]

    assert len(rows) == 2
    assert {r["protocol_version"] for r in rows} == {"v1.2", "v1.3"}
    assert all(r["run_count"] == 1 for r in rows)


def test_cost_unavailable_is_reported_not_shown_as_zero(
    client: TestClient, session: Session, world: World
) -> None:
    """报不出成本的次数要单独给出来（协议纪律 3）。

    claude-code 走中转端点时 44 次全报 `unavailable`，总额就是 0 ——
    前端没有这个计数就会显示"$0.00"，读起来是不花钱。
    """
    config = world.contestant("claude-code", "claude-code@deepseek-chat", kind=AgentKind.CLI)
    world.run(config, resolved=5, cost="0", cost_source=CostSource.UNAVAILABLE)
    session.commit()

    row = client.get("/api/leaderboard").json()["rows"]["items"][0]

    assert row["cost_unavailable_attempts"] == SNAPSHOT_TASKS
    assert row["cost_reported_attempts"] == 0
    assert Decimal(row["cost_usd_total"]) == Decimal("0")
    # 每题成本是"不知道"，不是 0 —— 不然按成本排时它会以"免费"夺冠
    assert row["cost_per_task"] is None


def test_facets_break_the_score_down_by_difficulty(
    client: TestClient, session: Session, world: World
) -> None:
    """`facet=difficulty` 给出每个难度上的成绩（§16.2 的 Leaderboard 页要分面）。

    取值原样透出（`easy` / `hard`），不做中文映射 —— 映射是展示层的事（AC-10）。
    """
    config = world.contestant("aider", "aider@deepseek-chat", kind=AgentKind.CLI)
    world.run(config, resolved=3)  # 前 3 道解决，而前 3 道正好都是 easy
    session.commit()

    body = client.get("/api/leaderboard", params={"facet": "difficulty"}).json()

    assert body["facet"] == "difficulty"
    facets = {cell["value"]: cell for cell in body["rows"]["items"][0]["facets"]}
    assert facets["easy"]["resolved"] == 3
    assert facets["easy"]["total"] == 3
    assert Decimal(facets["easy"]["resolve_rate"]) == Decimal("1.0000")
    assert facets["hard"]["resolved"] == 0


def test_facet_by_language_and_repository_also_work(
    client: TestClient, session: Session, world: World
) -> None:
    config = world.contestant("aider", "aider@deepseek-chat", kind=AgentKind.CLI)
    world.run(config, resolved=3)
    session.commit()

    by_language = client.get("/api/leaderboard", params={"facet": "language"}).json()
    by_repo = client.get("/api/leaderboard", params={"facet": "repository"}).json()

    assert {c["value"] for c in by_language["rows"]["items"][0]["facets"]} == {"zh", "en"}
    assert [c["value"] for c in by_repo["rows"]["items"][0]["facets"]] == ["pallets/click"]


def test_metric_cost_reorders_the_board(client: TestClient, session: Session, world: World) -> None:
    """按成本排时，便宜的在前 —— 哪怕它解决率低。"""
    cheap = world.contestant("cheap", "cheap@m", kind=AgentKind.CLI)
    pricey = world.contestant("pricey", "pricey@m", kind=AgentKind.CLI)
    world.run(cheap, resolved=1, cost="0.10")
    world.run(pricey, resolved=5, cost="9.00")
    session.commit()

    by_rate = labels(client.get("/api/leaderboard").json())
    by_cost = labels(client.get("/api/leaderboard", params={"metric": "cost"}).json())

    assert by_rate == ["pricey@m", "cheap@m"]
    assert by_cost == ["cheap@m", "pricey@m"]


def test_response_states_how_it_filtered(
    client: TestClient, session: Session, world: World
) -> None:
    """榜单要能自证：六条准入规则随响应一起返回。

    一个不说自己筛掉了什么的排行榜没法复核 —— 而这里恰好有四条规则
    是协议里没有的，不写出来谁都不知道。
    """
    config = world.contestant("aider", "aider@deepseek-chat", kind=AgentKind.CLI)
    world.run(config, resolved=1)
    session.commit()

    body = client.get("/api/leaderboard").json()

    assert len(body["eligibility"]) == 6
    assert any("C-28" in rule for rule in body["eligibility"])
    assert any("C-26b" in rule for rule in body["eligibility"])
    assert any("哨兵" in rule for rule in body["eligibility"])


def test_query_count_does_not_grow_with_contestants(
    client: TestClient, session: Session, world: World, engine: Engine
) -> None:
    """榜单的 SQL 条数不随参赛者数量增长（AC-7）。"""
    first = world.contestant("a", "a@m", kind=AgentKind.CLI)
    world.run(first, resolved=1)
    session.commit()
    with count_queries(engine) as few:
        assert len(labels(client.get("/api/leaderboard").json())) == 1

    for name in ("b", "c", "d", "e"):
        world.run(world.contestant(name, f"{name}@m", kind=AgentKind.CLI), resolved=2)
    session.commit()
    with count_queries(engine) as many:
        assert len(labels(client.get("/api/leaderboard").json())) == 5

    assert few.count == many.count, many.statements


def test_leaderboard_without_any_run_is_404_with_a_useful_message(
    client: TestClient, session: Session
) -> None:
    """一个实验都没有时说清楚该怎么办，不是返回一张空表让人猜。"""
    response = client.get("/api/leaderboard")

    assert response.status_code == 404
    assert response.json()["code"] == "BENCHMARK_SET_NOT_FOUND"
    assert "set=" in response.json()["message"]
