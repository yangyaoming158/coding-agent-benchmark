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
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.cost import TokenPrices
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
    #: 平台统一成本估算使用的三档美元单价。
    token_prices: TokenPrices = field(default_factory=TokenPrices)


#: 单价随时间变化，正式实验前需核对并创建独立配置；当前模型按高峰价保守估算。
DEEPSEEK_FLASH_PRICES = TokenPrices(
    input_per_mtok=Decimal("0.30"),
    output_per_mtok=Decimal("1.20"),
    cache_read_per_mtok=Decimal("0.006"),
)
#: 2026-09-10 以前 pilot 使用的 deepseek-chat 价目，只服务历史同条件配置。
#: ⚠ 2026-09-20 实测：`deepseek-chat` 已从 `/models` 下线，但两个端点仍返回 200 —— 它成了
#: **别名，实际路由到 deepseek-flash**（响应 model 字段 = deepseek-flash / deepseek-v4-flash）。
#: 所以 `@deepseek-chat` 两份配置只留给 pilot（#125–#128）的历史记录用；再拿它们跑实验，
#: manifest 里的 model_name 和价目都会和实际不符。最终实验用下面的 `@deepseek-flash`。
DEEPSEEK_CHAT_PRICES = TokenPrices(
    input_per_mtok=Decimal("0.27"),
    output_per_mtok=Decimal("1.10"),
    cache_read_per_mtok=Decimal("0.07"),
)

#: 2026-09-20 起 max_tokens_budget 30_000 → 300_000、max_turns 20 → 30。
#: 30_000 是 E3-T6 验收 Golden 一题时定的；runtime 按"消息历史字节数 + 1024"预留下一轮
#: 输入，字节数比 token 数多约 3 倍，30_000 实际只够 4–5 轮，真题上连定位 bug 都不够，
#: 而 aider / claude-code 没有这个上限（平台侧 `max_tokens_budget=None`）。
#: 300_000 按 flash 高峰价最坏 $0.09 / 题（缓存命中后远低于此），116 题 × 2 轮个位数美元。
MINIAGENT_PARAMS: dict[str, Any] = {
    "image": "bench-base:py311",
    "max_turns": 30,
    "max_output_tokens": 2048,
    "max_tokens_budget": 300_000,
    "thinking": "disabled",
    "price_source": "https://api-docs.deepseek.com/quick_start/pricing/ (2026-09-19 peak)",
}

SEED_AGENTS: tuple[SeedAgent, ...] = (
    SeedAgent(
        name="miniagent",
        display_name="MiniAgent（自研）",
        kind=AgentKind.CUSTOM,
        adapter_class="app.runner.adapters.miniagent.MiniAgentRunner",
        config_label="miniagent@deepseek-flash",
        note="四工具 ReAct、原生 JSONL、逐轮 token 与单价估算成本（E3-T6）",
        model_name="deepseek/deepseek-flash",
        params=MINIAGENT_PARAMS,
        token_prices=DEEPSEEK_FLASH_PRICES,
    ),
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
        params={
            "image": "bench-agent:py311-aider",
            "price_source": "https://api-docs.deepseek.com/news/news1226/ (historical)",
        },
        token_prices=DEEPSEEK_CHAT_PRICES,
    ),
    SeedAgent(
        name="claude-code",
        display_name="Claude Code",
        kind=AgentKind.CLI,
        adapter_class="app.runner.adapters.claude_code.ClaudeCodeRunner",
        config_label="claude-code@deepseek-chat",
        note="第二个真实被测 AI（E3-T5），headless 模式 + stream-json 轨迹",
        # 模型名写 DeepSeek 而不是 claude-*：这份配置让 Claude Code 打
        # DeepSeek 的 Anthropic 兼容端点。报表上"Agent 是 claude-code、
        # 模型是 deepseek-chat"就是事实 —— 写成 claude 系的名字才是撒谎。
        # 想跑官方端点就再加一份 config（base_url 留空、模型名换成 claude-*），
        # 同一个适配器接不同底座，和 Aider 那条路一样
        model_name="deepseek-chat",
        params={
            "image": "bench-agent:py311-claude-code",
            "base_url": "https://api.deepseek.com/anthropic",
            "max_turns": 40,
            "price_source": "https://api-docs.deepseek.com/news/news1226/ (historical)",
        },
        token_prices=DEEPSEEK_CHAT_PRICES,
    ),
    # ── 最终实验（E10-T4）用的两份：同一个 Agent 的第二份配置 ──
    # `name` 和上面相同，`seed_agents()` 会找到已有的 Agent 行、只新建配置行。
    # 三个参赛者（aider / claude-code / miniagent）底座模型全是 deepseek-flash，
    # 对比的就纯粹是 Agent 框架本身的差异。
    # 跑的时候要给 --config，见 `app.evaluation.agent_configs`。
    SeedAgent(
        name="aider",
        display_name="Aider",
        kind=AgentKind.CLI,
        adapter_class="app.runner.adapters.aider.AiderRunner",
        config_label="aider@deepseek-flash",
        note="最终实验配置（2026-09-20）：底座 deepseek-flash，关思考，价目按 flash 高峰价",
        model_name="deepseek/deepseek-flash",
        params={
            "image": "bench-agent:py311-aider",
            # deepseek-flash 默认开思考，aider 一题输出 37K token、成本 ×3，且和关了思考的
            # claude-code / MiniAgent 不可比。文件在 images/aider/model-settings/，只认文件名
            "model_settings": "deepseek-flash-no-thinking.yml",
            "price_source": "https://api-docs.deepseek.com/quick_start/pricing/ (2026-09-19 peak)",
        },
        token_prices=DEEPSEEK_FLASH_PRICES,
    ),
    SeedAgent(
        name="claude-code",
        display_name="Claude Code",
        kind=AgentKind.CLI,
        adapter_class="app.runner.adapters.claude_code.ClaudeCodeRunner",
        config_label="claude-code@deepseek-flash",
        note="最终实验配置（2026-09-20）：底座 deepseek-flash，价目按 flash 高峰价",
        model_name="deepseek-flash",
        params={
            "image": "bench-agent:py311-claude-code",
            "base_url": "https://api.deepseek.com/anthropic",
            "max_turns": 40,
            "price_source": "https://api-docs.deepseek.com/quick_start/pricing/ (2026-09-19 peak)",
        },
        token_prices=DEEPSEEK_FLASH_PRICES,
    ),
)


def seed_agents(session: Session) -> tuple[int, int]:
    """写入哨兵 Agent 与配置，返回（新建数，更新数）。

    按 name / label 查重，不靠固定主键 —— 固定主键在多人各自建库时会撞上。
    同一个 `name` 出现多次表示同一个 Agent 的多份配置：Agent 行只建一次（后面的
    条目算"更新"），配置行按 label 各建一份。
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
                    price_input_per_mtok=spec.token_prices.input_per_mtok,
                    price_output_per_mtok=spec.token_prices.output_per_mtok,
                    price_cache_read_per_mtok=spec.token_prices.cache_read_per_mtok,
                    config_hash=f"{spec.config_label:_<64}"[:64],
                    enabled=True,
                )
            )
            created += 1
        else:
            config.model_name = spec.model_name
            config.params = {"note": spec.note, **spec.params}
            config.price_input_per_mtok = spec.token_prices.input_per_mtok
            config.price_output_per_mtok = spec.token_prices.output_per_mtok
            config.price_cache_read_per_mtok = spec.token_prices.cache_read_per_mtok
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
