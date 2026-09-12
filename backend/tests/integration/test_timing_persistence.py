"""`load_attempts()` 那条查询（E9-T1）。

要一个真的 PostgreSQL，因为要证明的就是那三个 join 接得对：
执行 → 实验 → Agent 配置 → Agent，外加执行 → 题目。聚合那一半全是纯函数，
在 `tests/unit/test_timing.py` 里。

为什么值得单开一条集成测试：`A` 是按 **Agent 分组**算的，而 Agent 的名字要顺着
三张表才拿得到。join 写错的表现不是报错，是**两个 Agent 的耗时被混进同一个均值** ——
而混在一起的均值看上去完全正常。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.domain.enums import (
    AgentKind,
    AgentOutcome,
    EvaluationRunStatus,
    InfraOutcome,
    LifecycleStatus,
)
from app.evaluation.timing import load_attempts, summarize
from app.infrastructure.models.agent import Agent, AgentConfig
from app.infrastructure.models.evaluation import EvaluationRun, EvaluationTaskRun
from tests.integration.factories import seed_minimal

BASE = datetime(2026, 9, 12, 10, 0, 0, tzinfo=UTC)


def _add_agent(session: Session, name: str) -> int:
    """再加一个 Agent 配置，返回 `agent_configs.id`。"""
    agent = Agent(
        name=name,
        display_name=name,
        kind=AgentKind.CLI,
        adapter_class=f"app.runner.adapters.{name}.Runner",
    )
    session.add(agent)
    session.flush()
    config = AgentConfig(
        agent_id=agent.id,
        label=f"{name}@deepseek-chat",
        agent_version="builtin",
        model_name="deepseek-chat",
        config_hash=name.ljust(64, "0")[:64],
    )
    session.add(config)
    session.flush()
    return config.id


def _add_run(session: Session, *, benchmark_set_id: int, agent_config_id: int) -> int:
    run = EvaluationRun(
        name="pilot",
        benchmark_set_id=benchmark_set_id,
        agent_config_id=agent_config_id,
        status=EvaluationRunStatus.COMPLETED,
        total_tasks=1,
    )
    session.add(run)
    session.flush()
    return run.id


def _add_task_run(
    session: Session,
    *,
    run_id: int,
    task_id: int,
    agent_ms: int,
    total_ms: int,
    infra_outcome: InfraOutcome = InfraOutcome.SUCCESS,
    agent_outcome: AgentOutcome = AgentOutcome.RESOLVED,
) -> None:
    """写一条执行记录。

    `agent_outcome` 要显式给对：`ck_evaluation_task_runs_legal_combination`
    （协议 C-78 的组合表）会拦下非法组合，`COMPLETED + AGENT_TIMEOUT` 只允许
    配 `UNRESOLVED`。这道约束第一次写这个测试时就拦住了我 —— 它是在干活的。
    """
    session.add(
        EvaluationTaskRun(
            evaluation_run_id=run_id,
            benchmark_task_id=task_id,
            attempt_no=1,
            lifecycle_status=LifecycleStatus.COMPLETED,
            infra_outcome=infra_outcome,
            agent_outcome=agent_outcome,
            queued_at=BASE,
            prepare_started_at=BASE + timedelta(seconds=1),
            agent_started_at=BASE + timedelta(seconds=3),
            agent_finished_at=BASE + timedelta(seconds=3, milliseconds=agent_ms),
            completed_at=BASE + timedelta(seconds=1, milliseconds=total_ms),
            agent_duration_ms=agent_ms,
            total_duration_ms=total_ms,
        )
    )
    session.flush()


def test_load_attempts_keeps_each_agents_timings_apart(session: Session) -> None:
    """两个 Agent 各跑一次，`A` 必须分开算。

    join 写错的话两个会混进同一个均值：`(120 + 600) / 2 = 360` 秒，看起来
    像一个很正常的 `A=6 分钟`，而真相是一个 2 分钟、一个 10 分钟。
    """
    seeded = seed_minimal(session, tasks=1, slug="benchmark-dev")
    aider_config = _add_agent(session, "aider")
    claude_config = _add_agent(session, "claude-code")

    aider_run = _add_run(
        session, benchmark_set_id=seeded.benchmark_set_id, agent_config_id=aider_config
    )
    claude_run = _add_run(
        session, benchmark_set_id=seeded.benchmark_set_id, agent_config_id=claude_config
    )
    _add_task_run(
        session, run_id=aider_run, task_id=seeded.task_ids[0], agent_ms=120_000, total_ms=130_000
    )
    _add_task_run(
        session, run_id=claude_run, task_id=seeded.task_ids[0], agent_ms=600_000, total_ms=612_000
    )
    session.commit()

    attempts = load_attempts(session, [aider_run, claude_run])
    assert len(attempts) == 2

    by_agent = {item.agent_name: item for item in summarize(attempts)}
    assert set(by_agent) == {"aider", "claude-code"}
    assert by_agent["aider"].agent_minutes == 2.0
    assert by_agent["claude-code"].agent_minutes == 10.0
    # S = total − agent：aider 10 秒、claude-code 12 秒
    assert by_agent["aider"].other_minutes * 60 == 10.0
    assert by_agent["claude-code"].other_minutes * 60 == 12.0


def test_load_attempts_carries_the_task_id_and_outcome(session: Session) -> None:
    """题号和 `infra_outcome` 都要带出来 —— 超时率要用 outcome 算，CSV 要用题号。"""
    seeded = seed_minimal(session, tasks=1, slug="benchmark-dev")
    config_id = _add_agent(session, "aider")
    run_id = _add_run(session, benchmark_set_id=seeded.benchmark_set_id, agent_config_id=config_id)
    _add_task_run(
        session,
        run_id=run_id,
        task_id=seeded.task_ids[0],
        agent_ms=720_000,
        total_ms=725_000,
        infra_outcome=InfraOutcome.AGENT_TIMEOUT,
        agent_outcome=AgentOutcome.UNRESOLVED,  # C-18 的映射：超时算 AI 没修好
    )
    session.commit()

    (attempt,) = load_attempts(session, [run_id])
    assert attempt.task_id == "bench-golden__textkit-1"
    assert attempt.infra_outcome == InfraOutcome.AGENT_TIMEOUT
    assert attempt.timed_out

    (timing,) = summarize([attempt])
    assert timing.timeouts == 1
    assert timing.timeout_rate == 1.0


def test_load_attempts_of_no_runs_does_not_query(session: Session) -> None:
    """空列表直接返回空。不加这一条，`IN ()` 会在某些后端上语法错。"""
    assert load_attempts(session, []) == []


def test_load_attempts_only_returns_the_runs_asked_for(session: Session) -> None:
    """别的实验的执行不能漏进来 —— pilot 的统计只能数 pilot 自己那几个实验。"""
    seeded = seed_minimal(session, tasks=1, slug="benchmark-dev")
    config_id = _add_agent(session, "aider")
    wanted = _add_run(session, benchmark_set_id=seeded.benchmark_set_id, agent_config_id=config_id)
    other = _add_run(session, benchmark_set_id=seeded.benchmark_set_id, agent_config_id=config_id)
    _add_task_run(
        session, run_id=wanted, task_id=seeded.task_ids[0], agent_ms=60_000, total_ms=70_000
    )
    _add_task_run(
        session, run_id=other, task_id=seeded.task_ids[0], agent_ms=999_000, total_ms=999_000
    )
    session.commit()

    attempts = load_attempts(session, [wanted])
    assert [item.evaluation_run_id for item in attempts] == [wanted]
    assert session.scalar(sa.select(sa.func.count()).select_from(EvaluationTaskRun)) == 2
