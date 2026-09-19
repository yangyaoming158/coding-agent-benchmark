"""add Markdown and JSON report artifact kinds

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-20 00:00:00.000000+00:00

E10-T3 会把 HTML、Markdown、JSON 三种报告分别登记为制品。原有枚举只有
``REPORT_HTML``，强行复用会让制品索引说谎，所以补齐另外两个取值。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """补齐 Markdown 与 JSON 报告制品类型。"""
    op.execute(
        "ALTER TYPE artifact_kind ADD VALUE IF NOT EXISTS 'REPORT_MARKDOWN' "
        "BEFORE 'VALIDATION_EVIDENCE'"
    )
    op.execute(
        "ALTER TYPE artifact_kind ADD VALUE IF NOT EXISTS 'REPORT_JSON' "
        "BEFORE 'VALIDATION_EVIDENCE'"
    )


def downgrade() -> None:
    """把新类型折回 HTML 后重建旧枚举，保留已有制品索引。"""
    op.execute(
        "UPDATE artifacts SET kind = 'REPORT_HTML' WHERE kind IN ('REPORT_MARKDOWN', 'REPORT_JSON')"
    )
    op.execute("ALTER TYPE artifact_kind RENAME TO artifact_kind_with_reports")
    op.execute(
        "CREATE TYPE artifact_kind AS ENUM ("
        "'AGENT_STDOUT', 'AGENT_STDERR', 'TEST_STDOUT', 'TEST_REPORT_XML', "
        "'TRAJECTORY', 'PATCH', 'REPORT_HTML', 'VALIDATION_EVIDENCE', 'BUILD_LOG')"
    )
    op.execute(
        "ALTER TABLE artifacts ALTER COLUMN kind TYPE artifact_kind USING kind::text::artifact_kind"
    )
    op.execute("DROP TYPE artifact_kind_with_reports")
