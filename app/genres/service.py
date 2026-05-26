from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Genre
from app.exceptions import ConflictError, NotFoundError


def _slugify(name: str) -> str:
    """Derive a URL-safe slug from a display name.

    Examples: "Science Fiction" → "science-fiction"; "Self-Help" → "self-help".

    Kept intentionally simple — no Unicode normalization. If someone creates a genre with
    non-ASCII characters the slug will collapse the non-alnum stretches to hyphens, which
    is fine for the case-insensitive uniqueness check downstream.
    """
    s = re.sub(r"[^\w]+", "-", name.casefold(), flags=re.UNICODE).strip("-")
    return s or "genre"  # pathological all-punctuation name → fall back to a constant


async def get_genre(session: AsyncSession, genre_id: UUID) -> Genre:
    row = await session.get(Genre, genre_id)
    if row is None:
        raise NotFoundError("Genre", genre_id)
    return row


async def create_genre(session: AsyncSession, *, name: str) -> Genre:
    """Strict create. Returns 409 on case-insensitive duplicate of either name or slug
    (since two distinct names could collapse to the same slug — e.g. "Sci Fi" and
    "Sci-Fi" both → "sci-fi")."""
    slug = _slugify(name)

    # Cheap pre-check for a friendlier 409 in the common case. The DB unique indexes are
    # the authority — race-condition inserts land as IntegrityError below.
    existing = await session.execute(
        select(Genre).where(
            (func.lower(Genre.name) == name.casefold()) | (func.lower(Genre.slug) == slug)
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise ConflictError(f"Genre '{name}' already exists")

    genre = Genre(name=name, slug=slug)
    session.add(genre)
    try:
        await session.flush()
    except IntegrityError as e:
        raise ConflictError(f"Genre '{name}' already exists") from e
    return genre


async def update_genre(session: AsyncSession, genre_id: UUID, *, name: str) -> Genre:
    genre = await get_genre(session, genre_id)
    new_slug = _slugify(name)
    if name == genre.name and new_slug == genre.slug:
        return genre  # no-op rename

    # Pre-check excluding self.
    conflict = await session.execute(
        select(Genre).where(
            Genre.id != genre.id,
            (func.lower(Genre.name) == name.casefold()) | (func.lower(Genre.slug) == new_slug),
        )
    )
    if conflict.scalar_one_or_none() is not None:
        raise ConflictError(f"Genre '{name}' already exists")

    genre.name = name
    genre.slug = new_slug
    try:
        await session.flush()
    except IntegrityError as e:
        raise ConflictError(f"Genre '{name}' already exists") from e
    return genre


async def delete_genre(session: AsyncSession, genre_id: UUID) -> None:
    """Hard delete. The `books.genre_id ON DELETE RESTRICT` FK ensures we never silently
    orphan books — an attempt to delete a genre still in use raises IntegrityError, which
    the global handler converts to 409."""
    genre = await get_genre(session, genre_id)
    await session.delete(genre)
    try:
        await session.flush()
    except IntegrityError as e:
        raise ConflictError(
            "Cannot delete genre while books reference it. Reassign or delete those books first."
        ) from e
