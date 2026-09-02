"""健康检查接口。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.api.app import create_app
from app.api.health import check_database
from app.domain.protocol import PROTOCOL_VERSION
from app.infrastructure.db import create_db_engine

pytestmark = pytest.mark.db


def test_health_reports_database_and_protocol(engine: Engine) -> None:
    """服务起来之后，一个接口就能看清三件事：连没连上库、迁移到哪一版、依据哪版协议。"""
    with TestClient(create_app()) as client:
        body = client.get("/api/health").json()

    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["protocol_version"] == PROTOCOL_VERSION
    assert body["migration_revision"] == "0001"


def test_health_is_degraded_without_database() -> None:
    """连不上库时要明确说 degraded，不能装作一切正常。

    健康检查最没用的形态就是"永远返回 ok" —— 那它挡不住任何事故。
    """
    dead = create_db_engine("postgresql+psycopg://nobody:nobody@127.0.0.1:1/none")
    reachable, revision = check_database(dead)
    assert reachable is False
    assert revision is None
