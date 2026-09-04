"""集成测试的数据库夹具。

需要一个真的 PostgreSQL。本地起法：`./scripts/dev_db.sh up`。
连不上就整体跳过，不让没起数据库的人被一堆红叉挡住。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from alembic import command
from app.infrastructure.db import create_db_engine, get_database_url

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def alembic_config(url: str) -> Config:
    """建 alembic 配置。不带下划线是因为别的测试文件也要用它取当前 head 版本。"""
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture(scope="session")
def database_url() -> str:
    return get_database_url()


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    """建好表结构的引擎。

    表结构走真正的迁移建，不用 `metadata.create_all()`。
    差别很关键：`create_all` 只能证明模型自洽，证明不了迁移脚本能跑通，
    而上线时跑的是迁移脚本。
    """
    eng = create_db_engine(database_url)
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(
            f"连不上数据库 {database_url}，先跑 ./scripts/dev_db.sh up（{exc.__class__.__name__}）"
        )

    config = alembic_config(database_url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """每个测试一个事务，结束时整体回滚，测试之间互不影响。"""
    connection = engine.connect()
    transaction = connection.begin()
    db_session = Session(bind=connection)
    try:
        yield db_session
    finally:
        # 测试里如果故意触发过 IntegrityError，Session 的事务已经作废，
        # 先让它自己回滚，再收外层事务，否则会报 transaction already deassociated
        db_session.rollback()
        db_session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
