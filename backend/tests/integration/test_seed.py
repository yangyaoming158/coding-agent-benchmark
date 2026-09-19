"""种子数据脚本的检查。"""

from __future__ import annotations

from decimal import Decimal

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
    miniagent = session.scalar(
        select(AgentConfig).where(AgentConfig.label == "miniagent@deepseek-flash")
    )
    assert miniagent is not None
    assert miniagent.price_input_per_mtok == Decimal("0.3000")
    assert miniagent.price_output_per_mtok == Decimal("1.2000")
    assert miniagent.price_cache_read_per_mtok == Decimal("0.0060")
    assert "prices_usd_per_mtok" not in miniagent.params


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
