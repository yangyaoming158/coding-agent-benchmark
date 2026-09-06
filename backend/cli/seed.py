"""种子数据：三个哨兵 Agent 及其配置。

哨兵 Agent 是整条评测链能自测的关键 —— 它们不调用任何外部服务，
所以在没有大模型额度、没有网络的情况下也能把平台跑通：

- **Oracle**：交出官方补丁。在一个健康的题库上解决率必须是 **100%**。
  不是 100% 就说明有坏题，或者判定引擎有 bug。
- **Noop**：交出空补丁。解决率必须是 **0%**。
  不是 0% 说明有的题在修复前测试就已经通过了，这道题本身没有区分度。
- **Mock**：行为可编程（正确补丁 / 错误补丁 / 空补丁 / 超时 / 非法补丁 /
  改受保护文件），用来构造各种失败路径。

前两个是题库发布的硬门槛（协议 C-50），所以它们必须在库里有稳定的记录，
不能每次测试临时造。

用法：`python -m cli.seed`（可重复执行，已存在的记录只更新展示字段）
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.enums import AgentKind
from app.infrastructure.db import create_db_engine, create_session_factory, session_scope
from app.infrastructure.models import Agent, AgentConfig


@dataclass(frozen=True)
class SeedAgent:
    name: str
    display_name: str
    kind: AgentKind
    adapter_class: str
    config_label: str
    note: str
    #: 这份配置用哪个模型。哨兵一个模型都不调，写 `none` 而不是留空 ——
    #: 留空会让报表里出现一堆"未知模型"。
    model_name: str = "none"
    #: 适配器私有配置，原样进 `agent_configs.params`。
    params: Mapping[str, Any] = field(default_factory=dict)


SEED_AGENTS: tuple[SeedAgent, ...] = (
    SeedAgent(
        name="oracle",
        display_name="Oracle 哨兵",
        kind=AgentKind.ORACLE,
        adapter_class="app.runner.adapters.oracle.OracleRunner",
        config_label="oracle@gold",
        note="交官方补丁，解决率必须 100%",
    ),
    SeedAgent(
        name="noop",
        display_name="Noop 哨兵",
        kind=AgentKind.NOOP,
        adapter_class="app.runner.adapters.noop.NoopRunner",
        config_label="noop@empty",
        note="交空补丁，解决率必须 0%",
    ),
    SeedAgent(
        name="mock",
        display_name="Mock（行为可编程）",
        kind=AgentKind.MOCK,
        adapter_class="app.runner.adapters.mock.MockRunner",
        config_label="mock@programmable",
        note="按配置触发六种行为，用来测失败路径",
    ),
    SeedAgent(
        name="aider",
        display_name="Aider",
        kind=AgentKind.CLI,
        adapter_class="app.runner.adapters.aider.AiderRunner",
        config_label="aider@deepseek-chat",
        note="第一个真实被测 AI（E3-T4），在容器里改工作区",
        model_name="deepseek/deepseek-chat",
        # 镜像写进 params 而不是新开一列：同一个 Aider 接不同底座模型时，
        # 镜像是同一个，而 E2-T3 的分层构建器到位后这里会换成 bench-agent:<env>-aider，
        # 那时改的是数据不是表结构
        params={"image": "bench-agent:py311-aider"},
    ),
)


def seed_agents(session: Session) -> tuple[int, int]:
    """写入哨兵 Agent 与配置，返回（新建数，更新数）。

    按 name / label 查重，不靠固定主键 —— 固定主键在多人各自建库时会撞上。
    """
    created = 0
    updated = 0

    for spec in SEED_AGENTS:
        agent = session.scalar(select(Agent).where(Agent.name == spec.name))
        if agent is None:
            agent = Agent(
                name=spec.name,
                display_name=spec.display_name,
                kind=spec.kind,
                adapter_class=spec.adapter_class,
                is_domestic=False,
            )
            session.add(agent)
            session.flush()
            created += 1
        else:
            agent.display_name = spec.display_name
            agent.kind = spec.kind
            agent.adapter_class = spec.adapter_class
            updated += 1

        config = session.scalar(select(AgentConfig).where(AgentConfig.label == spec.config_label))
        if config is None:
            session.add(
                AgentConfig(
                    agent_id=agent.id,
                    label=spec.config_label,
                    agent_version="builtin",
                    # 哨兵不调用任何模型，这里给一个明确的占位值，
                    # 而不是留空 —— 留空会让报表里出现一堆 "未知模型"。
                    model_name=spec.model_name,
                    params={"note": spec.note, **spec.params},
                    config_hash=f"{spec.config_label:_<64}"[:64],
                    enabled=True,
                )
            )
            created += 1
        else:
            config.model_name = spec.model_name
            config.params = {"note": spec.note, **spec.params}
            updated += 1

    return created, updated


def main() -> None:
    engine = create_db_engine()
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        created, updated = seed_agents(session)
    print(f"种子数据完成：新建 {created} 条，更新 {updated} 条")


if __name__ == "__main__":
    main()
