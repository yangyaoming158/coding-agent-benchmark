"""Agent 与 Agent 配置的接口（E7-T0）。

两个端点分开，因为它们是两种东西：

- `GET /api/agents` —— **适配器定义**。一个 Agent 是一份接入代码（aider、claude-code）。
- `GET /api/agent-configs` —— **排行榜上的参赛者**。Agent × 模型 × 参数的一个具体组合。
  同一个 aider 接三个模型就是三个参赛者，但适配器代码只有一份。

§16.2 的 Agents 页要一起显示，所以配置行里直接带上它所属 Agent 的名字和类型 ——
前端不用为每一行再发一次请求去换名字（AC-7 的 N+1）。

## "probe 状态"这一页有、接口里没有

§16.2 的 Agents 页写着要显示"model、单价、版本、probe 状态"。前三个都有列，
**probe 状态在整个数据库里没有落点** —— `agents` 和 `agent_configs` 两张表
都没有这个字段，别处也没有这张表。

这张卡不建表（AC 里没有），所以接口不返回这个字段。E7-T1 做 Agents 页时
会发现它缺，那时再决定是加一列还是去掉这个展示项。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import Page, PageDep, SessionDep, page_of
from app.api.errors import READ_ERROR_RESPONSES
from app.domain.enums import AgentKind
from app.infrastructure.models.agent import Agent, AgentConfig

router = APIRouter(tags=["agents"])


class AgentSummary(BaseModel):
    """一个适配器定义。"""

    id: int
    name: str
    display_name: str
    #: 枚举原样透出（AC-10）。ORACLE / NOOP / MOCK 是哨兵，不是参赛者。
    kind: AgentKind
    adapter_class: str
    homepage: str | None
    is_domestic: bool
    created_at: datetime


class AgentConfigSummary(BaseModel):
    """一个参赛者。"""

    id: int
    agent_id: int
    agent_name: str
    agent_display_name: str
    agent_kind: AgentKind
    #: 展示用短名，如 `aider@deepseek-chat`。
    label: str
    agent_version: str
    model_name: str
    params: dict[str, Any]
    #: 每百万 token 的单价（美元）。没配就是 None，不编 0 ——
    #: 0 会被读成"免费"，而实际含义是"我们不知道"。
    price_input_per_mtok: Decimal | None
    price_output_per_mtok: Decimal | None
    config_hash: str
    #: 停用的参赛者不进排行榜。库里的 `aider@deepseek-chat+autotest` 就是
    #: 一次性诊断用的，停用着。
    enabled: bool
    created_at: datetime


@router.get("/api/agents", responses=READ_ERROR_RESPONSES)
def list_agents(
    session: SessionDep,
    page: PageDep,
    kind: Annotated[AgentKind | None, Query(description="只看这一类")] = None,
) -> Page[AgentSummary]:
    """适配器列表，按 id 升序。"""
    where = [Agent.kind == kind] if kind is not None else []
    total = int(
        session.execute(sa.select(sa.func.count()).select_from(Agent).where(*where)).scalar_one()
    )
    rows = (
        session.execute(
            sa.select(Agent).where(*where).order_by(Agent.id).limit(page.limit).offset(page.offset)
        )
        .scalars()
        .all()
    )
    return page_of(
        [AgentSummary.model_validate(row, from_attributes=True) for row in rows],
        total=total,
        params=page,
    )


@router.get("/api/agent-configs", responses=READ_ERROR_RESPONSES)
def list_agent_configs(
    session: SessionDep,
    page: PageDep,
    agent: Annotated[str | None, Query(description="只看这个 Agent 的配置，用 name")] = None,
    enabled: Annotated[bool | None, Query(description="只看启用/停用的")] = None,
) -> Page[AgentConfigSummary]:
    """参赛者列表，按 id 升序。一条 SQL 带出所属 Agent 的名字和类型。"""
    where: list[sa.ColumnElement[bool]] = []
    if agent is not None:
        where.append(Agent.name == agent)
    if enabled is not None:
        where.append(AgentConfig.enabled.is_(enabled))

    joined = sa.select(AgentConfig, Agent.name, Agent.display_name, Agent.kind).join(
        Agent, Agent.id == AgentConfig.agent_id
    )
    total = int(
        session.execute(
            sa.select(sa.func.count())
            .select_from(AgentConfig)
            .join(Agent, Agent.id == AgentConfig.agent_id)
            .where(*where)
        ).scalar_one()
    )
    rows = session.execute(
        joined.where(*where).order_by(AgentConfig.id).limit(page.limit).offset(page.offset)
    ).all()
    return page_of(
        [
            AgentConfigSummary(
                id=config.id,
                agent_id=config.agent_id,
                agent_name=name,
                agent_display_name=display_name,
                agent_kind=kind,
                label=config.label,
                agent_version=config.agent_version,
                model_name=config.model_name,
                params=dict(config.params or {}),
                price_input_per_mtok=config.price_input_per_mtok,
                price_output_per_mtok=config.price_output_per_mtok,
                config_hash=config.config_hash,
                enabled=config.enabled,
                created_at=config.created_at,
            )
            for config, name, display_name, kind in rows
        ],
        total=total,
        params=page,
    )


__all__ = ["AgentConfigSummary", "AgentSummary", "router"]
