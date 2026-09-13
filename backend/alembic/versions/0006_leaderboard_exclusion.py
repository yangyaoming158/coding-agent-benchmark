"""evaluation_runs 加一列：人工排除出排行榜的理由

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-12 00:00:00.000000+00:00

E7-T0 写 `/api/leaderboard` 时逼出来的。协议里已有的两条准入规则挡不住一类数据：

| 规则 | 挡的是 |
|:---|:---|
| C-26 / C-26b | 平台故障率超过 5% 的实验（落库记 `PARTIAL`） |
| C-28 | 工作区不干净时跑的实验（`dirty = true`） |

库里的 #119–#122 两条都不占：`status=COMPLETED`、`infra_failure_count=0`、
`dirty=false`、22 道题全有结论。按 C-26 和 C-28 筛完它们**完全合格**。

但那四行不是测量结果。那是 DeepSeek 余额耗尽的那一轮，88 次运行一次模型都没调到，
`402 Insufficient Balance` 当时被判成 `AGENT_RUNTIME_ERROR`（算被测 AI 的错、
不计入平台故障率），于是一个 0% 解决率大摇大摆进了排行榜。归类 bug 已经修掉，
之后同类运行会按 C-26b 记 `PARTIAL`，但这四行是修之前落的，**故意留着当证据**
（`07-platform-architecture.md` §18.6 第九节）。

所以要一个地方记"这次实验不进排行榜，因为 ……"。

**为什么是一列，不是排行榜查询里的一条启发式规则。**
现算的规则（比如"整场 22 题 token 全是 0 就算没跑"）今天能挑出这四行，代价是两条：
一是它把一条协议没有的准入规则藏进了 SQL，谁都不知道排行榜按什么筛；
二是将来真出现一个"一次模型都没调就交了空补丁"的参赛者，它会被静默误杀。
记成一列之后，排除是一条**写下来的、带理由的、能撤销的**事实，和 `dirty` 同一个性质。

**存文本不存 bool**：排除是要向人解释的动作，只记一个 true，
半年后没人说得清当初为什么排。

**不动那四行原有的判定字段**：`infra_outcome` 是适配器跑的时候写下的一次性判断，
没有重新推导的路，而且这次改的不是协议（版本号还是 v1.2），谈不上"按新协议重算"。
加一条注，不重写测量结果。

怎么填：`python -m cli.experiment exclude --run 119 --reason "..."`，
撤销用 `include --run 119`。没做成 API 写端点 —— 它不在 E7-T0 的端点清单里，
而且这种要留痕的判断适合走命令行，不适合在网页上点一下就改掉。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evaluation_runs",
        sa.Column("leaderboard_excluded_reason", sa.String(300), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("evaluation_runs", "leaderboard_excluded_reason")
