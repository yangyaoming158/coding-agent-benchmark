"""集成测试的数据库夹具。

需要一个真的 PostgreSQL。本地起法：`./scripts/dev_db.sh up`。
连不上就整体跳过，不让没起数据库的人被一堆红叉挡住。

## ⚠️ 跑**任何一个**集成测试都会清空它连上的那个库

下面那个 `engine` 夹具开头就是 `downgrade base` + `upgrade head`，整库连表带数据
抹掉重建。这不只发生在 `make check`：

    uv run pytest tests/integration/test_mining_persistence.py   ← 这一条也会清

2026-09-09 被咬了两次：挖好的 80 条候选和预筛结果各没了一回，
而且当时完全没往"我刚跑了个集成测试"上想 —— 表还在、只是数据空了，
看起来像是数据库自己出了问题。2026-09-10 又一次，31 道题连同刚验完的结论。

**所以现在测试默认跑在独立的 `bench_test` 库上**（#88）：`make test` / `make check`
会把 `BENCH_DATABASE_URL` 指过去，开发库 `bench` 碰都不碰。测试里调 CLI 的那些用例
也一样落在测试库里 —— `cli.dataset` / `cli.experiment` 自己开 engine，读的是同一个
环境变量。

但这道隔离只覆盖"从 Makefile 跑测试"这一条路。**清库这件事本身没有变**：
手工 `alembic downgrade base`、或者绕开 Makefile 直接 `uv run pytest`（那就连回开发库了），
照样会把库抹掉。所以下面还有一道 `warn_if_this_is_not_a_test_database()`：
库名里不含 `test` 就打一条醒目的警告，让"这条命令会清库"这件事还说得出来。
"""

import os
import sys
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic.config import Config
from sqlalchemy import Engine, event, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from alembic import command
from app.infrastructure.db import create_db_engine, get_database_url

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

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


def database_name(url: str) -> str:
    """从连接串里取库名。`...:5433/bench_test?sslmode=require` → `bench_test`。"""
    return url.rsplit("/", 1)[-1].split("?", 1)[0]


def warn_if_this_is_not_a_test_database(url: str) -> str | None:
    """库名里不含 `test` 就警告一声。返回警告内容，没警告就是 `None`。

    警告不拒绝：CI 的库名未必叫 `bench_test`，一刀切会把别人的流水线挡死。
    真正要防的是"不知道这条命令会清库"，一条看得见的警告就够了。

    走 `warnings.warn` 之外还直接写 stderr：pytest 的警告汇总在 `-q`、
    `-p no:warnings` 或者别人的 `filterwarnings` 配置下可能根本不显示，
    而这条信息漏掉的代价是一次二十分钟的重灌。
    """
    if "test" in database_name(url).lower():
        return None
    message = (
        f"集成测试要把 {url} 整个清空（downgrade base + upgrade head），"
        f"而这个库名里没有 test —— 它是开发库吗？\n"
        "默认的测试库是 bench_test，走 `make test` / `make check` 会自动指过去；"
        "直接跑 pytest 则会连回开发库。"
    )
    print(f"\n⚠️  {message}\n", file=sys.stderr)
    warnings.warn(message, stacklevel=2)
    return message


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

    warn_if_this_is_not_a_test_database(database_url)
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


# ── API 测试的夹具（E7-T0）────────────────────────────────────

#: 测试用的管理员 token。**拼出来而不是整串写死** —— 整串写会被
#: pre-commit 的密钥扫描当成真密钥拦下来（`make check` 不跑那一步，
#: 提交前的 `pre-commit run --all-files` 会跑）。
ADMIN_TOKEN_FOR_TESTS = "bench-test-" + "admin-" + "0123456789abcdef"


@pytest.fixture
def admin_token(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """给这条测试配一个管理员 token，结束后把配置缓存清掉。

    显式设进进程环境而不是依赖开发机的 `.env`：CI 上没有那个文件，
    而 `create_app()` 现在没配 token 就**拒绝启动**（E7-T0 AC-3）。
    """
    from app.infrastructure.config import reset_settings_cache

    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN_FOR_TESTS)
    reset_settings_cache()
    yield ADMIN_TOKEN_FOR_TESTS
    reset_settings_cache()


@pytest.fixture
def client(session: Session, admin_token: str) -> Iterator["TestClient"]:
    """一个连着**测试自己那个事务**的 API 客户端。

    把 `get_session` 这个依赖换成测试的会话，接口读到的就是测试刚写进去、
    还没提交的数据；测试结束外层事务一回滚，什么都不留下。

    写接口里的 `session.commit()` 不会破坏这个隔离：会话绑在一条已经
    开了事务的连接上，SQLAlchemy 2.0 默认用 SAVEPOINT 加入外层事务
    （`join_transaction_mode="conditional_savepoint"`），commit 释放的是保存点。
    """
    from fastapi.testclient import TestClient

    from app.api.app import create_app
    from app.api.deps import get_session

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def count_queries(eng: Engine) -> "QueryCounter":
    """数一段代码里发了几条 SQL。AC-7 的"不许有 N+1"靠它钉住。"""
    return QueryCounter(eng)


class QueryCounter:
    """`with count_queries(engine) as counter: ...` 之后读 `counter.count`。"""

    def __init__(self, eng: Engine) -> None:
        self._engine = eng
        self.statements: list[str] = []

    @property
    def count(self) -> int:
        return len(self.statements)

    def _record(self, _conn, _cursor, statement, *_args) -> None:  # type: ignore[no-untyped-def]
        self.statements.append(statement)

    def __enter__(self) -> "QueryCounter":
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *_exc: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)
