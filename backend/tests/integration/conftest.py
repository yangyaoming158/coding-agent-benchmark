"""集成测试的数据库夹具。

需要一个真的 PostgreSQL。本地起法：`./scripts/dev_db.sh up`。
连不上就整体跳过，不让没起数据库的人被一堆红叉挡住。
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError
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


#: 设成 1 就跳过下面那道保护。留给"Worker 崩了、租约还挂着"的情况 ——
#: 租约最长 30 分钟，不给逃生口的话那半小时里一条集成测试都跑不了。
FORCE_RESET_ENV = "BENCH_TEST_FORCE_DB_RESET"


def refuse_if_a_worker_is_working(eng: Engine) -> None:
    """有 Worker 正拿着租约干活时，拒绝清库。

    这道保护是 2026-09-06 花了七分钟真跑之后加的：当时一个 Worker 正在跑
    Golden 集，我在另一个终端跑了 `make check`，下面那两行
    `downgrade base` + `upgrade head` 把整个库连表带数据一起抹了。
    Worker 那边的表现是 `lease_lost`（它的保护起作用了，结果被丢弃没写坏数据），
    但一轮真实实验就这么没了，而且要过一会儿才看得出来发生了什么。

    "记得别在 Worker 跑着的时候跑测试"这种约定救不了人 —— 写在交付说明里的
    同一条坑，我自己一天之内踩了第二次。所以把它变成一条会报错的规则。

    只看**没过期**的租约：过期的那些说明 Worker 已经不在了，清掉无所谓。
    表还不存在（全新的库）时什么都不做。
    """
    if os.environ.get(FORCE_RESET_ENV) == "1":
        return
    try:
        with eng.connect() as conn:
            busy = conn.execute(
                text(
                    "select count(*) from job_queue "
                    "where state = 'LEASED' and lease_expires_at > now()"
                )
            ).scalar_one()
    except (OperationalError, ProgrammingError):
        return  # 表还没建，没什么可保护的

    if busy:
        pytest.fail(
            f"有 {busy} 条作业正被 Worker 租着，集成测试会把库清空（downgrade base）。\n"
            "先停掉 Worker（Ctrl-C 一次即可优雅停机），或者等它跑完。\n"
            f"确认那些租约是崩溃残留的话，用 {FORCE_RESET_ENV}=1 跳过这道保护。",
            pytrace=False,
        )


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

    refuse_if_a_worker_is_working(eng)

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
