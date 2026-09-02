"""作业队列表。

队列直接建在 Postgres 里，不引 Redis（ADR-003）。三个理由：

1. **状态即领域**：单次执行的状态本身就是业务核心资产，前端要查、报告要用。
   用 Celery 会把"作业状态"和"评测状态"割裂成两套真相。
2. **两件事一起成功或一起失败**："领走这个作业"和"把任务状态改成执行中"
   可以放进同一个数据库事务。作业状态在 Redis、任务状态在 Postgres 的话，
   会出现"Redis 说跑完了、数据库说没跑"这种对不上的情况。
3. **可观测**：`SELECT * FROM job_queue` 就能看清一切。

领取用 `FOR UPDATE SKIP LOCKED`，多个 Worker 之间不会抢到同一条。
"""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import JobState, JobType
from app.infrastructure.models.base import Base, pg_enum, utc_now_column


class JobQueue(Base):
    """一条待处理或处理中的作业。"""

    __tablename__ = "job_queue"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    job_type: Mapped[JobType] = mapped_column(pg_enum(JobType), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    priority: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=0)
    state: Mapped[JobState] = mapped_column(
        pg_enum(JobState), nullable=False, default=JobState.PENDING
    )
    attempts: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=3)
    #: 领走这条作业的 Worker 标识。
    lease_owner: Mapped[str | None] = mapped_column(sa.String(100))
    #: 租约到期时间。Worker 每 60 秒心跳续一次；过期还没做完的会被回收器
    #: 重置为 PENDING（还有重试次数）或标成 DEAD。
    lease_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    #: 最早可以被领取的时间。重试退避直接写成 now() + 2^attempts × 30 秒。
    available_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = utc_now_column()

    __table_args__ = (
        # 领取时的查询条件就是这三列，顺序也要一致。
        sa.Index("ix_job_queue_pick", "state", "available_at", "priority"),
        # 僵尸回收器只扫 LEASED 的行，用部分索引，避免扫全表。
        sa.Index(
            "ix_job_queue_expired_leases",
            "lease_expires_at",
            postgresql_where=sa.text("state = 'LEASED'"),
        ),
    )


__all__ = ["JobQueue"]
