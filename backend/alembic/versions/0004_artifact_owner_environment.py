"""add ENVIRONMENT to artifact_owner_type

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-08 00:00:00.000000+00:00

镜像构建日志要在 `artifacts` 表里有一行索引（E2-T3）。

`ArtifactKind.BUILD_LOG` 和 `environment_specs.build_log_uri` 从 0001 起就存在，
`07-platform-architecture.md` §17.2 也早就把 `envs/{environment_id}/build.log.gz`
列进了制品命名规范 —— 唯独 `artifact_owner_type` 里没有 ENVIRONMENT，
于是 BUILD_LOG 这个取值一直没法用：制品表是多态外键（owner_type + owner_id），
挂不到环境上就只能不入表。

加上它之后，构建日志和依赖锁与别的制品走同一套索引、同一套完整性校验，
将来 `bench artifacts gc` 也能一起管。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 0001 建这个类型时的四个取值，回滚要还原成这一份。
#: 写死不 import：迁移是历史快照，从代码 import 会让旧迁移用上新枚举。
OLD_VALUES: tuple[str, ...] = ("TASK", "TASK_RUN", "EVAL_RUN", "VALIDATION")
NEW_VALUE = "ENVIRONMENT"


def upgrade() -> None:
    """加一个枚举取值。

    `ADD VALUE IF NOT EXISTS` 在 PostgreSQL 12+ 可以在事务里跑（早期版本不行），
    本项目要求 PG 16，没问题。
    """
    op.execute(sa.text(f"ALTER TYPE artifact_owner_type ADD VALUE IF NOT EXISTS '{NEW_VALUE}'"))


def downgrade() -> None:
    """把取值去掉。

    PostgreSQL 没有 `ALTER TYPE ... DROP VALUE`，只能新建一个类型、把列改过去、
    删掉旧类型、再改回名字。四步一个都不能少。

    动手之前先删掉用了这个取值的行：留着它们，`ALTER COLUMN ... TYPE` 会因为
    "ENVIRONMENT 不是新类型的合法值"直接失败，而且报错信息指不到是哪几行。
    删掉的是构建日志的索引行，磁盘上的制品文件不受影响。
    """
    op.execute(sa.text(f"DELETE FROM artifacts WHERE owner_type = '{NEW_VALUE}'"))
    values = ", ".join(f"'{v}'" for v in OLD_VALUES)
    op.execute(sa.text(f"CREATE TYPE artifact_owner_type_old AS ENUM ({values})"))
    op.execute(
        sa.text(
            "ALTER TABLE artifacts ALTER COLUMN owner_type TYPE artifact_owner_type_old "
            "USING owner_type::text::artifact_owner_type_old"
        )
    )
    op.execute(sa.text("DROP TYPE artifact_owner_type"))
    op.execute(sa.text("ALTER TYPE artifact_owner_type_old RENAME TO artifact_owner_type"))
