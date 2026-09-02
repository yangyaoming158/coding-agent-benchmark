"""Agent 域的表：被测 AI 及其具体配置。"""

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import AgentKind
from app.infrastructure.models.base import Base, pg_enum, utc_now_column


class Agent(Base):
    """一个被测 AI 的适配器定义。

    注意这**不是**排行榜上的参赛者 —— 参赛者是下面的 `AgentConfig`。
    """

    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    kind: Mapped[AgentKind] = mapped_column(pg_enum(AgentKind), nullable=False)
    #: 适配器实现类的导入路径，如 `app.runner.adapters.aider.AiderRunner`。
    adapter_class: Mapped[str] = mapped_column(sa.String(300), nullable=False)
    homepage: Mapped[str | None] = mapped_column(sa.String(500))
    is_domestic: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = utc_now_column()


class AgentConfig(Base):
    """Agent × 模型 × 参数的一个具体组合 —— **这才是排行榜上的参赛者**。

    为什么要和 Agent 分开：同一个 Aider 接 3 个模型就是 3 个参赛者，
    但适配器代码只有 1 份。合成一张表的话，要么适配器信息重复 3 遍，
    要么就没法分别记录各自的单价和成绩。
    """

    __tablename__ = "agent_configs"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False
    )
    #: 展示用的短名，如 `aider@deepseek-chat`。
    label: Mapped[str] = mapped_column(sa.String(200), nullable=False, unique=True)
    agent_version: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    #: temperature / max_turns 这类调用参数，结构随 Agent 不同而不同，放 JSONB。
    params: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    #: 每百万 token 的单价（美元）。Agent 不报费用时按 token 用量 × 单价估算，
    #: 并把 cost_source 标成 estimated，报告里必须和真实上报的费用区分显示。
    price_input_per_mtok: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 4))
    price_output_per_mtok: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 4))
    #: 配置内容的规范化哈希，用来判断两次实验用的是不是同一套参数。
    config_hash: Mapped[str] = mapped_column(sa.CHAR(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = utc_now_column()

    __table_args__ = (sa.Index("ix_agent_configs_agent_id", "agent_id"),)


__all__ = ["Agent", "AgentConfig"]
