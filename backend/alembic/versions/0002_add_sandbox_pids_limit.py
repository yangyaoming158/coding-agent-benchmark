"""add sandbox_pids_limit to benchmark_tasks

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-04 11:44:37.384236+00:00

任务 Schema（`docs/plan/03-benchmark-spec.md` §7.1）里有 `sandbox_pids_limit`，
0001 建表时漏了这一列。`sandbox_cpu` 和 `sandbox_memory_mb` 都有独立列，只有它没有。

三个都是起容器时要读的资源限额。存法不一致的话，E2-T2 得为其中一个写特例，
取不到还要兜个默认值 —— 又多一处"静默用错默认值"的地方。

变更记录见 issue #60。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: pids 上限的默认值，和 §7.1 的任务 Schema 一致。
DEFAULT_PIDS_LIMIT = "512"


def upgrade() -> None:
    """加列。

    给 `server_default` 是必需的：这一列不可空，而加列时表里可能已经有题目了，
    没有默认值的话 PostgreSQL 会直接拒绝（"column contains null values"）。
    现在表是空的，但迁移得在任何时候都能跑通 —— 别人从生产库升级时不该炸。
    """
    op.add_column(
        "benchmark_tasks",
        sa.Column(
            "sandbox_pids_limit",
            sa.Integer(),
            nullable=False,
            server_default=sa.text(DEFAULT_PIDS_LIMIT),
        ),
    )
    # 建完就把 server_default 摘掉，和 0001 里 sandbox_memory_mb 的做法保持一致：
    # 默认值由应用层（模型的 default=512）负责，数据库只管非空。
    # 两边都设默认值的话，改默认值要记得改两处，迟早对不上。
    op.alter_column("benchmark_tasks", "sandbox_pids_limit", server_default=None)


def downgrade() -> None:
    op.drop_column("benchmark_tasks", "sandbox_pids_limit")
