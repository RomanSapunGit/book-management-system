"""import_sessions table

Persists one row per bulk-import attempt. The endpoint at POST /books/import returns the
session id; clients can re-fetch the report from GET /books/import/{id}.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "import_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("format", sa.String(10), nullable=False),
        sa.Column("received", sa.Integer, nullable=False, server_default="0"),
        sa.Column("successful", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer, nullable=False, server_default="0"),
        # Stored truncated to a cap (writer enforces); the int alongside lets the client
        # know how many were elided.
        sa.Column(
            "errors", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("error_count_total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_import_sessions_user_id", "import_sessions", ["user_id"])
    # Compound index used by the rate-limit query: COUNT WHERE user_id = ? AND created_at >= ?
    op.create_index("ix_import_sessions_user_created", "import_sessions", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_import_sessions_user_created", table_name="import_sessions")
    op.drop_index("ix_import_sessions_user_id", table_name="import_sessions")
    op.drop_table("import_sessions")
