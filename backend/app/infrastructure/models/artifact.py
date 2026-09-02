"""制品索引表。

日志、轨迹、补丁全文、测试报告、HTML 报告这些东西一律**不入库**，
只在这里留一行索引 + 摘要。理由很实际：单个 Agent 的 stdout 可以到几 MB，
把它塞进数据库会让每次列表查询都变慢，而它 99% 的时间根本不需要被读取。
"""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import ArtifactBackend, ArtifactKind, ArtifactOwnerType
from app.infrastructure.models.base import Base, pg_enum, utc_now_column


class Artifact(Base):
    """一个制品的索引行。

    `owner_type` + `owner_id` 是多态外键：制品可能挂在题目、单次执行、
    整次实验或一次题目验证上。用多态而不是给每种拥有者建一张关联表，
    是因为制品种类还会增加，每次都建表不划算；代价是数据库层面没有外键约束，
    删除拥有者时要靠应用层清理。
    """

    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    owner_type: Mapped[ArtifactOwnerType] = mapped_column(
        pg_enum(ArtifactOwnerType), nullable=False
    )
    owner_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    kind: Mapped[ArtifactKind] = mapped_column(pg_enum(ArtifactKind), nullable=False)
    uri: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    backend: Mapped[ArtifactBackend] = mapped_column(
        pg_enum(ArtifactBackend), nullable=False, default=ArtifactBackend.LOCAL
    )
    content_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    #: 内容哈希，用于完整性校验与去重。
    sha256: Mapped[str] = mapped_column(sa.CHAR(64), nullable=False)
    #: 文本制品一律 gzip 压缩后存储，日志的压缩比常在 10:1。
    compressed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = utc_now_column()

    __table_args__ = (
        sa.Index("ix_artifacts_owner", "owner_type", "owner_id"),
        sa.Index("ix_artifacts_kind", "kind"),
    )


__all__ = ["Artifact"]
