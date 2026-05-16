"""drop name-uniqueness on authors

Authors are people. Two people can share a name (and, occasionally, even a birthdate).
The old `uq_authors_name` (exact) + `uq_authors_name_lower` (case-insensitive) indexes
encoded the false invariant "in our system, one person per name." Dropping both lets the
catalog hold "John Smith #1" and "John Smith #2" as distinct rows, with disambiguation
delegated to clients (via id) rather than enforced at the schema layer.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-16
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_authors_name_lower")
    op.drop_constraint("uq_authors_name", "authors", type_="unique")


def downgrade() -> None:
    # Re-adding may fail if duplicate rows now exist; that's expected — the operator must
    # dedupe by hand before downgrading.
    op.create_unique_constraint("uq_authors_name", "authors", ["name"])
    op.execute("CREATE UNIQUE INDEX uq_authors_name_lower ON authors (lower(name))")
