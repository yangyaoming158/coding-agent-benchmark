"""失败归因与人工复核的表。"""

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import (
    AttributionStage,
    AttributionStatus,
    FailureCategory,
    HumanReviewAction,
    ReportFormat,
    ReportScope,
)
from app.infrastructure.models.base import Base, pg_enum, utc_now_column


class FailureAttribution(Base):
    """一次执行失败的原因分类。

    一次执行只能有一条归因结论，所以外键上带唯一约束。规则层能判的
    （F6 回归、F7 空补丁、F8 超时、N1 平台故障）不调大模型 —— 这几类
    通常占失败的 30~50%，且规则准确率接近 100%，直接把总体准确率的底板抬高了。

    **大模型的输出禁止回写 agent_outcome**（协议 C-40）。它只用来分析原因。
    """

    __tablename__ = "failure_attributions"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    evaluation_task_run_id: Mapped[int] = mapped_column(
        sa.BigInteger,
        sa.ForeignKey("evaluation_task_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    stage: Mapped[AttributionStage] = mapped_column(pg_enum(AttributionStage), nullable=False)
    category: Mapped[FailureCategory] = mapped_column(pg_enum(FailureCategory), nullable=False)
    secondary_category: Mapped[FailureCategory | None] = mapped_column(pg_enum(FailureCategory))
    confidence: Mapped[Decimal | None] = mapped_column(sa.Numeric(4, 3))
    judge_model: Mapped[str | None] = mapped_column(sa.String(200))
    #: 提示词的哈希。换了提示词，历史归因结果就不再可比，靠这个字段能查出来。
    prompt_hash: Mapped[str | None] = mapped_column(sa.CHAR(64))
    #: 支撑这个结论的证据（改动文件集合、测试报错信息变化、轨迹特征等）。
    evidence: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    reasoning_zh: Mapped[str | None] = mapped_column(sa.Text)
    raw_response: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    status: Mapped[AttributionStatus] = mapped_column(
        pg_enum(AttributionStatus), nullable=False, default=AttributionStatus.OK
    )
    created_at: Mapped[datetime] = utc_now_column()

    __table_args__ = (
        sa.Index("ix_failure_attributions_category", "category"),
        sa.Index("ix_failure_attributions_status", "status"),
    )


class HumanReview(Base):
    """人工复核记录。

    `blind` 是盲检标志：复核时不告诉复核人自动归因给的是什么分类，
    否则人会不自觉地跟着走，抽检就失去了校验意义。
    """

    __tablename__ = "human_reviews"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    evaluation_task_run_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("evaluation_task_runs.id", ondelete="CASCADE"), nullable=False
    )
    reviewer: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    #: 抽检批次号。准确率和一致性（κ）按批次统计。
    sample_batch_id: Mapped[str | None] = mapped_column(sa.String(100))
    blind: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    action: Mapped[HumanReviewAction] = mapped_column(pg_enum(HumanReviewAction), nullable=False)
    corrected_category: Mapped[FailureCategory | None] = mapped_column(pg_enum(FailureCategory))
    comment: Mapped[str | None] = mapped_column(sa.Text)
    reviewed_at: Mapped[datetime] = utc_now_column()

    __table_args__ = (
        sa.Index("ix_human_reviews_sample_batch_id", "sample_batch_id"),
        sa.Index("ix_human_reviews_task_run", "evaluation_task_run_id"),
    )


class ReportRecord(Base):
    """生成过的报告。"""

    __tablename__ = "report_records"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    evaluation_run_id: Mapped[int | None] = mapped_column(
        sa.BigInteger, sa.ForeignKey("evaluation_runs.id", ondelete="CASCADE")
    )
    scope: Mapped[ReportScope] = mapped_column(pg_enum(ReportScope), nullable=False)
    #: 对比报告涉及的多次实验。单次实验报告这里只有一个元素。
    run_ids: Mapped[list[int]] = mapped_column(
        sa.ARRAY(sa.BigInteger), nullable=False, server_default=sa.text("'{}'::bigint[]")
    )
    format: Mapped[ReportFormat] = mapped_column(pg_enum(ReportFormat), nullable=False)
    artifact_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    params: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    generated_at: Mapped[datetime] = utc_now_column()


__all__ = ["FailureAttribution", "HumanReview", "ReportRecord"]
