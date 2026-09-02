"""基准域的表：仓库、环境规格、题目、数据集、挖掘候选。"""

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import (
    BenchmarkSetStatus,
    ImageBuildStatus,
    IssueLanguage,
    TaskCandidateState,
    TaskDifficulty,
    TaskValidationState,
)
from app.infrastructure.models.base import Base, pg_enum, utc_now_column


class Repository(Base):
    """被评测的开源仓库。"""

    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    full_name: Mapped[str] = mapped_column(sa.String(200), nullable=False, unique=True)
    url: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    default_branch: Mapped[str] = mapped_column(sa.String(100), nullable=False, default="main")
    language: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    stars: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    license: Mapped[str | None] = mapped_column(sa.String(100))
    #: 是不是国产开源项目。中文 issue 占比是本项目的公开指标，需要按这个维度分面统计。
    is_domestic: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    #: 本地 git 镜像路径。工作区物化走 `git archive` 从本地镜像导出，不每次联网 clone。
    mirror_path: Mapped[str | None] = mapped_column(sa.String(500))
    created_at: Mapped[datetime] = utc_now_column()


class EnvironmentSpec(Base):
    """环境规格 —— 一个可复现测试环境的逻辑定义，对应一个预建镜像。

    镜像按仓库预建（ADR-008），把装依赖的开销从"每次评测一遍"变成"每个环境一遍"。
    这是 300 次评测能在 6 小时内跑完的前提。
    """

    __tablename__ = "environment_specs"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    #: 人可读的稳定标识，如 `nonebot2__py311__v3`。
    environment_id: Mapped[str] = mapped_column(sa.String(200), nullable=False, unique=True)
    repository_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("repositories.id", ondelete="RESTRICT"), nullable=False
    )
    python_version: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    install_command: Mapped[str] = mapped_column(sa.Text, nullable=False)
    pre_test_command: Mapped[str | None] = mapped_column(sa.Text)
    test_command: Mapped[str] = mapped_column(sa.Text, nullable=False)
    test_framework: Mapped[str] = mapped_column(sa.String(50), nullable=False, default="pytest")
    test_report_path: Mapped[str] = mapped_column(sa.String(300), nullable=False)

    #: 该环境**额外追加**的受保护路径。
    #:
    #: 命名特意不叫 `protected_paths`：协议 C-61 规定环境规格只能在默认清单
    #: （C-42）上追加，**禁止**整体替换或删减。叫 `protected_paths` 会让人以为
    #: 这就是完整清单，某个仓库配错一次，防作弊就整体失效且不会报错。
    extra_protected_paths: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )

    image_tag: Mapped[str | None] = mapped_column(sa.String(300))
    #: 镜像内容哈希。协议 C-36 要求引用镜像用 digest 不用 tag —— tag 会被覆盖，digest 不会。
    image_digest: Mapped[str | None] = mapped_column(sa.String(100))
    build_status: Mapped[ImageBuildStatus] = mapped_column(
        pg_enum(ImageBuildStatus), nullable=False, default=ImageBuildStatus.PENDING
    )
    built_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    build_log_uri: Mapped[str | None] = mapped_column(sa.String(500))

    __table_args__ = (
        sa.Index("ix_environment_specs_repository_id", "repository_id"),
        sa.Index("ix_environment_specs_build_status", "build_status"),
    )


class BenchmarkTask(Base):
    """一道评测题：代码快照 + issue 描述 + 验证测试。

    `test_patch` 和 `gold_patch` 存**制品**而不是文本列：它们经常几十 KB，
    而且 gold_patch 属于"绝不能误发给被测 AI"的敏感内容（协议 C-44），
    放在独立存储更容易做访问控制。
    """

    __tablename__ = "benchmark_tasks"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    task_id: Mapped[str] = mapped_column(sa.String(200), nullable=False, unique=True)
    repository_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("repositories.id", ondelete="RESTRICT"), nullable=False
    )
    environment_spec_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("environment_specs.id", ondelete="RESTRICT"), nullable=False
    )
    #: 代码回退到的那个提交，也就是"bug 还在"的状态。
    base_commit: Mapped[str] = mapped_column(sa.CHAR(40), nullable=False)

    issue_title: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    issue_body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    issue_language: Mapped[IssueLanguage] = mapped_column(pg_enum(IssueLanguage), nullable=False)
    source_issue_url: Mapped[str | None] = mapped_column(sa.String(500))
    source_pr_url: Mapped[str | None] = mapped_column(sa.String(500))

    #: 修复前必须失败、修复后必须通过的用例 ID 列表。
    fail_to_pass: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    #: 修复前后都必须通过的用例 ID 列表，用来检查有没有把别的功能改坏。
    pass_to_pass: Mapped[list[str]] = mapped_column(JSONB, nullable=False)

    test_patch_uri: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    #: `test_patch` 实际改动的全部文件路径（协议 C-74）。
    #:
    #: 由 Validator 从 test_patch 推导，**不是**人工填写；仓库相对 POSIX 路径、
    #: 排序去重、rename 时新旧路径都记；纳入 content_hash；导入和验证时重算一遍，
    #: 对不上就拒绝该题（防有人手工放开某个文件的保护）。
    #:
    #: **禁止**下发给被测 AI（协议 C-76）—— 那等于直接告诉它官方测试补丁改了哪几个文件。
    test_patch_paths: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    gold_patch_uri: Mapped[str] = mapped_column(sa.String(500), nullable=False)

    difficulty: Mapped[TaskDifficulty] = mapped_column(pg_enum(TaskDifficulty), nullable=False)
    tags: Mapped[list[str]] = mapped_column(
        sa.ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'::text[]")
    )

    agent_timeout_s: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=720)
    test_timeout_s: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=480)
    sandbox_cpu: Mapped[Decimal] = mapped_column(
        sa.Numeric(4, 2), nullable=False, server_default=sa.text("2.0")
    )
    sandbox_memory_mb: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=2048)

    validation_state: Mapped[TaskValidationState] = mapped_column(
        pg_enum(TaskValidationState), nullable=False, default=TaskValidationState.DISCOVERED
    )
    invalid_reason_code: Mapped[str | None] = mapped_column(sa.String(100))
    validated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    validation_evidence_uri: Mapped[str | None] = mapped_column(sa.String(500))

    #: 题目内容的规范化哈希。它让"数据集版本"成为一个可验证的事实，
    #: 而不是靠"我记得当时是这样"。
    content_hash: Mapped[str] = mapped_column(sa.CHAR(64), nullable=False)
    #: 题目的原始 JSON 定义，结构会演化，且不需要 join 查询，所以放 JSONB。
    raw_definition: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[datetime] = utc_now_column()
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )

    __table_args__ = (
        sa.Index("ix_benchmark_tasks_validation_state", "validation_state"),
        sa.Index("ix_benchmark_tasks_repository_id", "repository_id"),
        sa.Index("ix_benchmark_tasks_difficulty", "difficulty"),
        sa.Index("ix_benchmark_tasks_issue_language", "issue_language"),
        sa.Index("ix_benchmark_tasks_tags", "tags", postgresql_using="gin"),
    )


class BenchmarkSet(Base):
    """数据集版本。"""

    __tablename__ = "benchmark_sets"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    version: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[BenchmarkSetStatus] = mapped_column(
        pg_enum(BenchmarkSetStatus), nullable=False, default=BenchmarkSetStatus.DRAFT
    )
    task_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = utc_now_column()

    __table_args__ = (
        sa.UniqueConstraint("slug", "version", name="uq_benchmark_sets_slug_version"),
    )


class BenchmarkSetItem(Base):
    """数据集与题目的快照关系。

    为什么要单独存 `task_content_hash`：数据集发布后，题目表里的那道题还可能被
    修正（比如补一条 P2P）。存下发布当时的哈希，就能事后判断"这次实验用的题
    和现在库里的题是不是同一个"。没有这一列，可复现性就只是一句口号。
    """

    __tablename__ = "benchmark_set_items"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    benchmark_set_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("benchmark_sets.id", ondelete="CASCADE"), nullable=False
    )
    benchmark_task_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("benchmark_tasks.id", ondelete="RESTRICT"), nullable=False
    )
    task_content_hash: Mapped[str] = mapped_column(sa.CHAR(64), nullable=False)
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    __table_args__ = (
        sa.UniqueConstraint(
            "benchmark_set_id", "benchmark_task_id", name="uq_benchmark_set_items_set_task"
        ),
    )


class TaskCandidate(Base):
    """挖掘出来的候选 PR，还没成为正式题目。

    与 `benchmark_tasks` 分表，避免几千条噪声候选污染正式题目表。
    """

    __tablename__ = "task_candidates"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    repository_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    pr_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    issue_number: Mapped[int | None] = mapped_column(sa.Integer)
    raw_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    prescreen_score: Mapped[Decimal | None] = mapped_column(sa.Numeric(5, 3))
    prescreen_reason: Mapped[str | None] = mapped_column(sa.Text)
    state: Mapped[TaskCandidateState] = mapped_column(
        pg_enum(TaskCandidateState), nullable=False, default=TaskCandidateState.DISCOVERED
    )
    reject_reason: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = utc_now_column()

    __table_args__ = (
        sa.UniqueConstraint("repository_id", "pr_number", name="uq_task_candidates_repo_pr"),
        sa.Index("ix_task_candidates_state", "state"),
    )


__all__ = [
    "BenchmarkSet",
    "BenchmarkSetItem",
    "BenchmarkTask",
    "EnvironmentSpec",
    "Repository",
    "TaskCandidate",
]
