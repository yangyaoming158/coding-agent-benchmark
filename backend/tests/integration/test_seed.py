"""种子数据脚本的检查。"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.enums import AgentKind
from app.infrastructure.models import Agent, AgentConfig
from cli.seed import SEED_AGENTS, seed_agents

pytestmark = pytest.mark.db


def test_seed_creates_sentinel_agents(session: Session) -> None:
    """三个哨兵 Agent 和它们的配置都要建出来。"""
    created, updated = seed_agents(session)
    session.flush()

    assert (created, updated) == (len(SEED_AGENTS) * 2, 0)
    kinds = set(session.scalars(select(Agent.kind)))
    assert {AgentKind.ORACLE, AgentKind.NOOP, AgentKind.MOCK} <= kinds


def test_seed_is_idempotent(session: Session) -> None:
    """重复执行不会产生重复记录。

    这条很实际：种子脚本会在每次重建开发库、每次部署时被跑一遍，
    不幂等的话第二次就会因为唯一约束直接失败。
    """
    seed_agents(session)
    session.flush()
    created, updated = seed_agents(session)
    session.flush()

    assert created == 0
    assert updated == len(SEED_AGENTS) * 2
    assert session.scalar(select(func.count()).select_from(Agent)) == len(SEED_AGENTS)
    assert session.scalar(select(func.count()).select_from(AgentConfig)) == len(SEED_AGENTS)
