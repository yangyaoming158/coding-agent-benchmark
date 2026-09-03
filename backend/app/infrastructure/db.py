"""数据库连接与会话。

用同步的 SQLAlchemy 会话，不用 async。理由：评测是重 IO 没错，但等待发生在
Docker 容器和大模型 API 上，不在数据库上；Worker 是独立进程，用同步会话
写起来简单得多，也不用担心 async 上下文里误调阻塞函数。
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.infrastructure.config import get_settings


def get_database_url() -> str:
    """取数据库连接串（来自 `BENCH_DATABASE_URL`，默认值和端口说明见 config.py）。

    配置读一次就缓存住。测试里要改指向，改完环境变量调
    `app.infrastructure.config.reset_settings_cache()`。
    """
    return get_settings().database_url


def create_db_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """建引擎。

    `pool_pre_ping` 是必要的：Worker 进程可能在两次作业之间闲置很久，
    中间连接被数据库或防火墙掐掉，不 ping 一下会在下次查询时才发现。
    """
    return create_engine(url or get_database_url(), echo=echo, pool_pre_ping=True, future=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """建会话工厂。

    `expire_on_commit=False` 是给 Worker 用的：提交之后还要读对象的字段来写日志，
    默认行为会让每个字段都触发一次重新查询。
    """
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """一个事务范围。正常结束就提交，抛异常就回滚。"""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
