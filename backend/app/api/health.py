"""健康检查接口。

存在的理由不只是"给探针用"：`make dev` 起来之后，第一件想确认的事就是
"数据库连上了吗、迁移跑到最新了吗、代码依据的是哪版协议"。
把这三件事放在一个接口里，比翻三处日志快得多。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.domain.protocol import PROTOCOL_VERSION

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """健康检查的返回。"""

    status: Literal["ok", "degraded"]
    #: 代码当前依据的协议版本（协议 C-67：实验创建时要把它写进 evaluation_runs）
    protocol_version: str
    database: Literal["ok", "unreachable"]
    #: 数据库当前的迁移版本号。为 None 表示还没跑过迁移。
    migration_revision: str | None


def check_database(engine: Engine) -> tuple[bool, str | None]:
    """探一下数据库，顺便把当前迁移版本读出来。

    读 `alembic_version` 而不是只做 `SELECT 1`：连得上但没跑迁移，
    对这个服务来说和连不上一样是不能干活的状态，得能区分出来。
    """
    try:
        with engine.connect() as conn:
            revision = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
    except SQLAlchemyError:
        return False, None
    return True, revision


@router.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """服务是否就绪。"""
    from app.api.app import get_engine

    reachable, revision = check_database(get_engine())
    return HealthResponse(
        status="ok" if reachable and revision else "degraded",
        protocol_version=PROTOCOL_VERSION,
        database="ok" if reachable else "unreachable",
        migration_revision=revision,
    )
