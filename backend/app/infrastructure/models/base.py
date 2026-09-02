"""所有 ORM 模型的公共基类与小工具。"""

from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.enums import ALL_DB_ENUMS

#: 约束命名规则。不定这个的话，Alembic 自动生成的迁移里约束名是数据库随机给的，
#: 下次要删改某个约束时就没法在迁移脚本里稳定引用它。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """ORM 基类。"""

    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


#: 枚举类 → 数据库类型名。把 ALL_DB_ENUMS 反过来查，保证两边只有一份真相。
_DB_TYPE_NAME: dict[type[StrEnum], str] = {v: k for k, v in ALL_DB_ENUMS.items()}


#: 已经建好的枚举类型对象。同一个枚举被多张表引用时必须复用同一个对象，
#: 否则 SQLAlchemy 会为每张表各发一次 CREATE TYPE，第二次就报"类型已存在"。
_ENUM_CACHE: dict[type[StrEnum], sa.Enum] = {}


def pg_enum(enum_cls: type[StrEnum]) -> sa.Enum:
    """把 Python 枚举映射成 PostgreSQL 原生枚举类型。

    两个必须这么写的地方：

    - `values_callable`：SQLAlchemy 默认存枚举成员的**名字**，不是取值。
      本项目大部分枚举名值相同无所谓，但 difficulty（`easy`）、issue_language（`zh`）、
      cost_source（`reported`）的取值是小写的，不指定就会存成大写，
      和任务 JSON 对不上。
    - `name`：显式指定数据库类型名，从 ALL_DB_ENUMS 查，
      迁移脚本回滚时按同一张表逐个 DROP TYPE。
    """
    cached = _ENUM_CACHE.get(enum_cls)
    if cached is None:
        cached = sa.Enum(
            enum_cls,
            name=_DB_TYPE_NAME[enum_cls],
            values_callable=lambda cls: [member.value for member in cls],
            native_enum=True,
        )
        _ENUM_CACHE[enum_cls] = cached
    return cached


def utc_now_column() -> Mapped[datetime]:
    """带时区的创建时间列，默认值由数据库给（`now()`），不由 Python 给。

    为什么用数据库时间：Worker 可能跑在不同机器上，机器之间的时钟会有偏差。
    时间戳用来算耗时和排序，必须来自同一个时钟。
    """
    return mapped_column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


class TimestampMixin:
    """创建时间 + 更新时间。"""

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )
