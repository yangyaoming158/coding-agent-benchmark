"""种子数据脚本的检查。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.enums import AgentKind
from app.evaluation.agent_configs import AgentConfigError, resolve_agent_config
from app.infrastructure.models import Agent, AgentConfig
from cli.seed import SEED_AGENTS, seed_agents

pytestmark = pytest.mark.db

#: 种子里 Agent 名字会重复（同一个 Agent 的多份配置），Agent 行数按去重后的名字数算
AGENT_NAMES = {spec.name for spec in SEED_AGENTS}
CONFIG_LABELS = [spec.config_label for spec in SEED_AGENTS]


def test_seed_creates_sentinel_agents(session: Session) -> None:
    """三个哨兵 Agent 和它们的配置都要建出来。"""
    created, updated = seed_agents(session)
    session.flush()

    # 每条 spec 建一份配置；Agent 行只在名字第一次出现时建，后面同名的算更新
    assert len(CONFIG_LABELS) == len(set(CONFIG_LABELS)), "配置标签必须唯一"
    assert created == len(AGENT_NAMES) + len(SEED_AGENTS)
    assert updated == len(SEED_AGENTS) - len(AGENT_NAMES)
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
    assert session.scalar(select(func.count()).select_from(Agent)) == len(AGENT_NAMES)
    assert session.scalar(select(func.count()).select_from(AgentConfig)) == len(SEED_AGENTS)


def test_final_experiment_configs_share_the_agent_row(session: Session) -> None:
    """aider / claude-code 各两份配置挂在同一个 Agent 行下，flash 那份带 flash 价目。"""
    seed_agents(session)
    session.flush()

    for name, labels in (
        ("aider", {"aider@deepseek-chat", "aider@deepseek-flash"}),
        ("claude-code", {"claude-code@deepseek-chat", "claude-code@deepseek-flash"}),
    ):
        agent = session.scalar(select(Agent).where(Agent.name == name))
        assert agent is not None
        configs = list(session.scalars(select(AgentConfig).where(AgentConfig.agent_id == agent.id)))
        assert {config.label for config in configs} == labels
        flash = next(config for config in configs if config.label.endswith("@deepseek-flash"))
        assert "flash" in flash.model_name
        assert flash.price_cache_read_per_mtok == Decimal("0.0060")

    miniagent = session.scalar(
        select(AgentConfig).where(AgentConfig.label == "miniagent@deepseek-flash")
    )
    assert miniagent is not None
    assert miniagent.params["max_tokens_budget"] == 300_000


def test_resolve_agent_config_requires_a_label_when_ambiguous(session: Session) -> None:
    """一个 Agent 一份配置时按名字就行；两份时必须给标签，且标签要属于那个 Agent。"""
    seed_agents(session)
    session.flush()

    assert resolve_agent_config(session, agent="oracle").label == "oracle@gold"
    assert (
        resolve_agent_config(session, agent="aider", config="aider@deepseek-flash").label
        == "aider@deepseek-flash"
    )

    with pytest.raises(AgentConfigError, match="2 份启用配置"):
        resolve_agent_config(session, agent="aider")
    with pytest.raises(AgentConfigError, match="不属于 --agent"):
        resolve_agent_config(session, agent="aider", config="claude-code@deepseek-flash")
    with pytest.raises(AgentConfigError, match="找不到配置"):
        resolve_agent_config(session, agent="aider", config="aider@nope")
    with pytest.raises(AgentConfigError, match="启用配置"):
        resolve_agent_config(session, agent="no-such-agent")

    # 停用一份之后，剩下那份又能按名字直接选到
    old = session.scalar(select(AgentConfig).where(AgentConfig.label == "aider@deepseek-chat"))
    assert old is not None
    old.enabled = False
    session.flush()
    assert resolve_agent_config(session, agent="aider").label == "aider@deepseek-flash"
