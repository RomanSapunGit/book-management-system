from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --- Catalog domain ---

book_authors = Table(
    "book_authors",
    Base.metadata,
    Column(
        "book_id",
        PG_UUID(as_uuid=True),
        ForeignKey("books.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "author_id",
        PG_UUID(as_uuid=True),
        ForeignKey("authors.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Index("ix_book_authors_author_id", "author_id"),
)


class Genre(Base):
    __tablename__ = "genres"
    __table_args__ = (
        UniqueConstraint("name", name="uq_genres_name"),
        UniqueConstraint("slug", name="uq_genres_slug"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)


class Author(Base):
    __tablename__ = "authors"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    books: Mapped[list[Book]] = relationship(
        "Book", secondary=book_authors, back_populates="authors", lazy="selectin"
    )


class Book(Base):
    __tablename__ = "books"
    __table_args__ = (
        # Lower bound is the brief's `>= 1800`. Upper bound (`<= current_year`) is enforced at the
        # Pydantic layer because Postgres CHECK constraints must be IMMUTABLE — `now()` isn't.
        CheckConstraint(
            "published_year IS NULL OR published_year >= 1800", name="ck_books_published_year_min"
        ),
        Index("ix_books_genre_id", "genre_id"),
        Index("ix_books_published_year", "published_year"),
        # `ix_books_title_lower` (functional, lower(title)) is created in the migration only.
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    genre_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("genres.id", ondelete="RESTRICT"), nullable=True
    )
    published_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    genre: Mapped[Genre | None] = relationship(Genre, lazy="joined")
    authors: Mapped[list[Author]] = relationship(
        Author, secondary=book_authors, back_populates="books", lazy="selectin"
    )


# --- Auth domain ---


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ImportSession(Base):
    """Persisted record of one bulk-import attempt.

    Lives in the DB (rather than memory) so:
      - clients can find the report via GET /books/import (paginated list)
      - rate limiting can count attempts per user per hour without separate infra
      - audit shows who imported what and when

    `errors` is a JSONB array of {row, reason}. Capped at 1000 entries in the writer to
    prevent unbounded growth from a CSV of 100k broken rows.
    """

    __tablename__ = "import_sessions"
    __table_args__ = (
        Index("ix_import_sessions_user_id", "user_id"),
        Index("ix_import_sessions_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    format: Mapped[str] = mapped_column(String(10), nullable=False)  # 'csv' | 'json'
    received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    successful: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    error_count_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RefreshToken(Base):
    """One row per issued refresh token.

    `token_hash` is sha256(raw_token). Refresh tokens are random 256-bit values, so a fast hash is
    correct here — argon2 would be a category error (slow hashing protects low-entropy passwords;
    refresh tokens are high-entropy and only need confidentiality at rest).
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_refresh_tokens_hash"),
        Index("ix_refresh_tokens_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When this token is rotated, store the id of the replacement. Reuse of an already-rotated
    # token signals theft → we revoke the whole user's tokens (see auth/service.refresh).
    replaced_by_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
