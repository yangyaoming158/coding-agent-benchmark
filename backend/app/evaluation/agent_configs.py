"""按 Agent 名字或配置标签找到要参赛的 `agent_configs` 行。

`cli.experiment start` 和 `cli.queue enqueue` 原来都是 `--agent <name>` 直接
`scalar_one_or_none()`。一个 Agent 只有一份配置时没问题；2026-09-20 起 aider 和
claude-code 各有两份（旧 `@deepseek-chat` 留作 pilot 的历史记录，新 `@deepseek-flash`
跑最终实验），再按名字取就会撞 `MultipleResultsFound`。

规则只有一条：**给了 `--config` 就按标签取，没给就要求该 Agent 恰好有一份启用的配置。**
多份时不猜、不取"最新的"——最终实验的命令要能从记录里原样复现，猜出来的配置做不到。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.infrastructure.models.agent import Agent, AgentConfig


class AgentConfigError(Exception):
    """找不到、有歧义或名字和标签对不上。消息可以直接打给用户看。"""


def resolve_agent_config(session: Session, *, agent: str, config: str | None = None) -> AgentConfig:
    """找到要用的配置行。

    - `config` 给了：按 `agent_configs.label` 精确取，并核对它确实属于 `agent`
      （防止 `--agent aider --config claude-code@…` 这种手误跑出一份标错名字的实验）
    - 没给：取该 Agent **启用中**的配置；恰好一份就用它，零份或多份都报错并列出标签
    """
    if config is not None:
        row = session.execute(
            sa.select(AgentConfig).join(Agent).where(AgentConfig.label == config)
        ).scalar_one_or_none()
        if row is None:
            raise AgentConfigError(f"找不到配置 {config!r}，先跑 `make seed` 或检查标签拼写")
        owner = session.get(Agent, row.agent_id)
        if owner is None or owner.name != agent:
            raise AgentConfigError(
                f"配置 {config!r} 属于 Agent {owner.name if owner else '?'!r}，"
                f"不属于 --agent {agent!r}"
            )
        return row

    rows = list(
        session.scalars(
            sa.select(AgentConfig)
            .join(Agent)
            .where(Agent.name == agent, AgentConfig.enabled.is_(True))
            .order_by(AgentConfig.id)
        )
    )
    if not rows:
        raise AgentConfigError(f"找不到 Agent {agent!r} 的启用配置，先跑 `make seed`")
    if len(rows) > 1:
        labels = "、".join(row.label for row in rows)
        raise AgentConfigError(
            f"Agent {agent!r} 有 {len(rows)} 份启用配置（{labels}），用 --config <标签> 指定一份"
        )
    return rows[0]


__all__ = ["AgentConfigError", "resolve_agent_config"]
