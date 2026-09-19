"""单机只允许一个 Worker 的数据库锁（E9-T3）。

本项目不做分布式 Worker。一台机器同时启动两个 Worker 时，后启动者原来会在
``startup_reaped_containers`` 里把前一个 Worker 的容器当成孤儿删除。用 PostgreSQL
会话级 advisory lock（顾问锁）把这条运维约定变成启动时的硬检查：持锁连接断开时，
数据库会自动释放锁，所以进程被 ``kill -9`` 后也不会留下永久锁。
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.infrastructure.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

logger = get_logger(__name__)

#: ``CABENCH`` 的 ASCII 十六进制，小于 PostgreSQL signed bigint 上限。
#: 用项目固定键而不是 worker_id：本项目明确只允许一个 Worker，两个不同名字也不能并存。
WORKER_LOCK_KEY = 0x434142454E4348


class WorkerAlreadyRunningError(RuntimeError):
    """数据库锁已被另一个 Worker 持有。"""


class SingleWorkerGuard:
    """在 Worker 生命周期内持有一个 PostgreSQL 会话级锁。"""

    def __init__(self, session_factory: sessionmaker[Session], *, worker_id: str) -> None:
        self._session_factory = session_factory
        self.worker_id = worker_id
        self._connection: Connection | None = None

    def acquire(self) -> None:
        """取得单 Worker 锁；已有 Worker 时立即报错，不等待。"""
        if self._connection is not None:
            return

        with self._session_factory() as session:
            bind = session.get_bind()
        if not isinstance(bind, Engine):
            raise RuntimeError("Worker 单实例锁需要绑定 SQLAlchemy Engine")

        if bind.dialect.name != "postgresql":
            # 生产只用 PostgreSQL；保留这条降级是为了纯单元测试可用 SQLite。
            logger.warning(
                "worker_singleton_lock_skipped",
                worker_id=self.worker_id,
                dialect=bind.dialect.name,
            )
            return

        connection = bind.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            acquired = bool(
                connection.execute(
                    sa.text("select pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": WORKER_LOCK_KEY},
                ).scalar_one()
            )
        except BaseException:
            connection.close()
            raise
        if not acquired:
            connection.close()
            logger.error(
                "worker_already_running",
                worker_id=self.worker_id,
                detail="已有 Worker 持有运行锁；拒绝启动第二个 Worker",
            )
            raise WorkerAlreadyRunningError("已有 Worker 持有运行锁；一台机器只能运行一个 Worker")

        self._connection = connection
        logger.info("worker_singleton_lock_acquired", worker_id=self.worker_id)

    def release(self) -> None:
        """主动释放锁；进程异常退出时 PostgreSQL 也会随连接释放。"""
        connection = self._connection
        if connection is None:
            return
        self._connection = None
        try:
            connection.execute(
                sa.text("select pg_advisory_unlock(:lock_key)"),
                {"lock_key": WORKER_LOCK_KEY},
            )
            logger.info("worker_singleton_lock_released", worker_id=self.worker_id)
        except Exception as exc:
            # 连接已经断了时锁也已经由 PostgreSQL 释放；这里不能盖掉 Worker 原来的异常。
            # 如果是解锁 SQL 自己失败，连接不能带着会话锁回到连接池。
            with contextlib.suppress(Exception):
                connection.invalidate()
            logger.warning(
                "worker_singleton_lock_release_failed",
                worker_id=self.worker_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            connection.close()

    def __enter__(self) -> SingleWorkerGuard:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


__all__ = ["WORKER_LOCK_KEY", "SingleWorkerGuard", "WorkerAlreadyRunningError"]
