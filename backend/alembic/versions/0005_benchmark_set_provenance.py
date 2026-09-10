"""benchmark_sets 补三列来源与发布证据

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-10 00:00:00.000000+00:00

E1-T6 要回答三个关于一个数据集版本的问题，而 0001 建的 `benchmark_sets`
一个都答不了：

| 问题 | 新列 |
|:---|:---|
| 这一版的题是按什么条件挑出来的 | `source_dataset_id` |
| 冻进去的到底是哪一批题、什么内容 | `snapshot_digest` |
| 凭什么允许它发布 | `publish_evidence` |

`source_dataset_id` 是被现状逼出来的：题目的归属只写在
`benchmark_tasks.raw_definition->>'dataset_id'` 里（`03-benchmark-spec.md` §8.11
第十节写了为什么不加列），而 `benchmark_sets.slug` 和它不是一回事 ——
库里 Golden 那批题的 `dataset_id` 是 `golden-v1`，对应的 set slug 却是 `golden`。
不记下来的话，"这一版的题是怎么挑出来的"就只存在于某个人的记忆里。

`snapshot_digest` 是 `benchmark_set_items` 的聚合哈希（算法见
`app/benchmark/dataset.py`）。它本身可以从 items 现算，存一份是为了**发现直接改库**：
现算的和存的对不上，说明有人绕过发布流程动了快照。

`publish_evidence` 记 Oracle / Noop 两次门禁实验的 id 和解决率（协议 C-50）。
不存的话只能拿 `benchmark_set_id` 去 `evaluation_runs` 里反查，而同一个数据集
以后还会跑很多次 Oracle，分不清哪两次是当初的门禁。

三列都可空：0001 建的行（DRAFT 的 golden）没有这些信息，回填不了也不该编。

还给 `benchmark_tasks` 加了第四列 `quarantine`，记一道题**为什么被隔离**
（`{at, from_state, reason}`）。

**为什么不塞进 `raw_definition`**：那一列是 `TaskDefinition` 的原文，`extra="forbid"`，
多一个键就再也解析不回来 —— 隔离过的题会连 `cli.validate run` 都跑不动。
而且 `content_hash` 算的就是 `raw_definition`，往里加东西等于悄悄改了题目的身份证。
隔离是题目的**状态**，不是题目的**内容**（同一条道理见 `hashing.py` 排除 `validation`）。

**为什么不复用 `invalid_reason_code`**：那七个 code 说的都是"八步验证跑出来不合格"
（§7.3），而隔离的理由是"发布后复验不通过"，硬套一个是在编 —— §8.11 第九节
对人工否掉也是这么处理的。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "benchmark_tasks",
        sa.Column("quarantine", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("benchmark_sets", sa.Column("source_dataset_id", sa.String(100), nullable=True))
    op.add_column("benchmark_sets", sa.Column("snapshot_digest", sa.CHAR(64), nullable=True))
    op.add_column(
        "benchmark_sets",
        sa.Column("publish_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("benchmark_tasks", "quarantine")
    op.drop_column("benchmark_sets", "publish_evidence")
    op.drop_column("benchmark_sets", "snapshot_digest")
    op.drop_column("benchmark_sets", "source_dataset_id")
