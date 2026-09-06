"""add tokens_cache_read to evaluation_task_runs

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-06 00:00:00.000000+00:00

Runner 协议 §9.2 的 `token_usage` 里一直有 `cache_read` 这一项，但 0001 建表时
只落了 `input` / `output` / `total` 三列，缓存命中数采到之后就扔了。

E3-T4 接第一个真实 Agent 时这件事才有了实际后果：DeepSeek 开着提示缓存，
实测四道 Golden 题每次都命中，每轮 2.4k 左右。缓存命中的单价比普通输入
便宜一个数量级，所以：

- 不记的话，"两次运行 token 差不多、钱差好几倍"解释不了；
- 按 token 估算成本那条路（协议纪律 3 的 `estimated`）会系统性偏高 ——
  把缓存命中按全价算进去了。

**这一列不进 `tokens_total`。** 缓存命中是 `input` 的一部分，不是另加的一份；
加进总数会让 token 统计凭空多出一截。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """加列。

    可空，不给 `server_default`：这一列的语义是"这次运行有多少 token 命中了缓存"，
    而**空和 0 是两个意思** —— 空是"这个适配器报不出来"（订阅制 CLI、
    我们的哨兵、E3-T4 之前落的所有历史行都是这种），0 是"报得出来，确实一次没命中"。
    给个默认 0 的话，历史行会被追认成"确实没命中过"，成本分析拿它当真值就错了。
    这和 `cost_usd` 那一列是同一条纪律。
    """
    op.add_column(
        "evaluation_task_runs",
        sa.Column("tokens_cache_read", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("evaluation_task_runs", "tokens_cache_read")
