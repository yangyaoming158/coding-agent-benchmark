"""add cache-read token price to agent_configs

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-19 00:00:00.000000+00:00

E5-T5 要求输入、输出和缓存读取三档单价分开。前两档在 0001 已有，
本迁移只补第三档；不修改 ``cost_source`` 枚举。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加缓存读取单价；历史配置未知，所以保持可空且不给默认值。"""
    op.add_column(
        "agent_configs",
        sa.Column("price_cache_read_per_mtok", sa.Numeric(precision=10, scale=4), nullable=True),
    )


def downgrade() -> None:
    """回滚缓存读取单价。"""
    op.drop_column("agent_configs", "price_cache_read_per_mtok")
