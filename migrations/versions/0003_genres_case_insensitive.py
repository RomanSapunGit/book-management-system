"""case-insensitive uniqueness on genres.name (mirrors authors/users)

The original migration created genres as a closed seeded set, so case-sensitive `UNIQUE(name)`
was sufficient — duplicates couldn't happen by definition. With `POST /genres` introduced,
"Fantasy" and "FANTASY" become two rows under that constraint. Adding a functional
unique index on `lower(name)` plugs the gap, matching the pattern already used for authors
and users.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-16
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE UNIQUE INDEX uq_genres_name_lower ON genres (lower(name))")
    op.execute("CREATE UNIQUE INDEX uq_genres_slug_lower ON genres (lower(slug))")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_genres_slug_lower")
    op.execute("DROP INDEX IF EXISTS uq_genres_name_lower")
