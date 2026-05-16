"""initial schema: genres, authors, books, book_authors, users, refresh_tokens

Hand-written. Schema lives here, not in `app/db/models.py`.

Revision ID: 0001
Revises:
Create Date: 2026-05-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


# The spec says "predefined list". We treat that as a closed set seeded by the migration.
# Adding a new genre is therefore a code change → review → migration. That's the point — if
# operators could add genres ad-hoc, the "predefined" property would not actually hold.
_SEED_GENRES = [
    ("Fiction", "fiction"),
    ("Non-Fiction", "non-fiction"),
    ("Fantasy", "fantasy"),
    ("Science Fiction", "science-fiction"),
    ("Mystery", "mystery"),
    ("Thriller", "thriller"),
    ("Romance", "romance"),
    ("Horror", "horror"),
    ("Biography", "biography"),
    ("History", "history"),
    ("Self-Help", "self-help"),
    ("Children", "children"),
    ("Young Adult", "young-adult"),
    ("Poetry", "poetry"),
    ("Drama", "drama"),
]


def upgrade() -> None:
    # gen_random_uuid() ships in PG 13+, but pgcrypto guarantees it on older versions too.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "genres",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.UniqueConstraint("name", name="uq_genres_name"),
        sa.UniqueConstraint("slug", name="uq_genres_slug"),
    )

    op.create_table(
        "authors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("bio", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("name", name="uq_authors_name"),
    )
    # Case-insensitive uniqueness — "Tolkien" and "TOLKIEN" must not coexist.
    op.execute("CREATE UNIQUE INDEX uq_authors_name_lower ON authors (lower(name))")

    op.create_table(
        "books",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column(
            "genre_id",
            postgresql.UUID(as_uuid=True),
            # RESTRICT: deleting a genre that has books is almost certainly a mistake.
            sa.ForeignKey("genres.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("published_year", sa.Integer, nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        # Lower bound only at the DB. Upper bound (≤ current_year) is at the Pydantic layer
        # because CHECK must be immutable.
        sa.CheckConstraint("published_year IS NULL OR published_year >= 1800", name="ck_books_published_year_min"),
    )
    op.create_index("ix_books_genre_id", "books", ["genre_id"])
    op.create_index("ix_books_published_year", "books", ["published_year"])
    op.execute("CREATE INDEX ix_books_title_lower ON books (lower(title))")

    op.create_table(
        "book_authors",
        sa.Column("book_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("books.id", ondelete="CASCADE"), primary_key=True),
        # RESTRICT, not CASCADE: deleting an author with books is data loss by accident.
        sa.Column("author_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("authors.id", ondelete="RESTRICT"), primary_key=True),
    )
    op.create_index("ix_book_authors_author_id", "book_authors", ["author_id"])

    # --- Auth ---
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    # Case-insensitive email uniqueness. Mixed-case emails should not become separate accounts.
    op.execute("CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email))")

    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])

    # --- updated_at trigger ---
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in ("authors", "books", "users"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at "
            f"BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
        )

    # --- Seed predefined genres ---
    genres_table = sa.table(
        "genres",
        sa.column("name", sa.String),
        sa.column("slug", sa.String),
    )
    op.bulk_insert(genres_table, [{"name": n, "slug": s} for n, s in _SEED_GENRES])


def downgrade() -> None:
    for table in ("users", "books", "authors"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table};")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at();")
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.execute("DROP INDEX IF EXISTS uq_users_email_lower;")
    op.drop_table("users")
    op.drop_index("ix_book_authors_author_id", table_name="book_authors")
    op.drop_table("book_authors")
    op.execute("DROP INDEX IF EXISTS ix_books_title_lower;")
    op.drop_index("ix_books_published_year", table_name="books")
    op.drop_index("ix_books_genre_id", table_name="books")
    op.drop_table("books")
    op.execute("DROP INDEX IF EXISTS uq_authors_name_lower;")
    op.drop_table("authors")
    op.drop_table("genres")
