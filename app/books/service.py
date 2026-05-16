from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.exceptions import NotFoundError
from app.db.models import Author, Book, Genre, book_authors

SORT_COLUMNS = {
    "title": Book.title,
    "published_year": Book.published_year,
    "created_at": Book.created_at,
    "updated_at": Book.updated_at,
}
SortField = Literal["title", "published_year", "created_at", "updated_at"]
SortDir = Literal["asc", "desc"]


@dataclass(slots=True)
class BookFilters:
    title: str | None = None
    author: str | None = None
    author_id: UUID | None = None
    genre: str | None = None  # by genre name (case-insensitive exact)
    genre_id: UUID | None = None
    year_from: int | None = None
    year_to: int | None = None


def apply_filters(stmt: Select, f: BookFilters) -> Select:
    if f.title:
        stmt = stmt.where(func.lower(Book.title).contains(f.title.casefold()))
    if f.genre_id is not None:
        stmt = stmt.where(Book.genre_id == f.genre_id)
    if f.genre:
        # Join via the relationship — single LEFT JOIN, no N+1.
        stmt = stmt.where(Book.genre.has(func.lower(Genre.name) == f.genre.casefold()))
    if f.year_from is not None:
        stmt = stmt.where(Book.published_year >= f.year_from)
    if f.year_to is not None:
        stmt = stmt.where(Book.published_year <= f.year_to)
    if f.author_id is not None:
        stmt = stmt.where(Book.authors.any(Author.id == f.author_id))
    if f.author:
        stmt = stmt.where(Book.authors.any(func.lower(Author.name).contains(f.author.casefold())))
    return stmt


def build_list_stmt(
    filters: BookFilters,
    sort_by: SortField = "created_at",
    sort_dir: SortDir = "desc",
) -> Select:
    """Build the unbounded list query (no limit/offset). `paginate()` adds those itself.

    Includes the `(sort, id)` ordering tiebreaker so pagination is deterministic — without
    `id`, two rows with the same `created_at` could swap pages between requests.
    """
    col = SORT_COLUMNS[sort_by]
    order = col.asc() if sort_dir == "asc" else col.desc()
    return (
        apply_filters(select(Book), filters)
        .options(selectinload(Book.authors))
        .order_by(order, Book.id)
    )


async def get_book(session: AsyncSession, book_id: UUID) -> Book:
    book = await session.get(Book, book_id, options=[selectinload(Book.authors)])
    if not book:
        raise NotFoundError("Book", book_id)
    return book


async def _validate_authors(session: AsyncSession, author_ids: list[UUID]) -> list[Author]:
    if not author_ids:
        return []
    rows = (
        await session.execute(select(Author).where(Author.id.in_(author_ids)))
    ).scalars().all()
    found = {a.id for a in rows}
    missing = [i for i in author_ids if i not in found]
    if missing:
        raise NotFoundError("Author", missing[0])
    by_id = {a.id: a for a in rows}
    return [by_id[i] for i in author_ids]


async def _validate_genre(session: AsyncSession, genre_id: UUID | None) -> None:
    if genre_id is None:
        return
    genre = await session.get(Genre, genre_id)
    if not genre:
        raise NotFoundError("Genre", genre_id)


async def create_book(
    session: AsyncSession,
    *,
    title: str,
    genre_id: UUID | None,
    published_year: int | None,
    description: str | None,
    author_ids: list[UUID],
) -> Book:
    await _validate_genre(session, genre_id)
    authors = await _validate_authors(session, author_ids)
    book = Book(
        title=title,
        genre_id=genre_id,
        published_year=published_year,
        description=description,
        authors=authors,
    )
    session.add(book)
    await session.flush()
    # Refresh the trigger-updated `updated_at` along with relationships, otherwise the
    # ORM holds a stale value (or worse, marks it expired → lazy load → MissingGreenlet).
    await session.refresh(book, attribute_names=["authors", "genre", "updated_at"])
    return book


async def update_book(session: AsyncSession, book_id: UUID, *, changes: dict) -> Book:
    """`changes` must come from `model.model_dump(exclude_unset=True)`."""
    book = await get_book(session, book_id)

    if "genre_id" in changes:
        await _validate_genre(session, changes["genre_id"])

    if "author_ids" in changes:
        author_ids = changes.pop("author_ids")
        if not author_ids:
            # Empty author list means "remove all authors" — almost certainly a bug, not a feature.
            from app.exceptions import ConflictError

            raise ConflictError("A book must have at least one author")
        book.authors = await _validate_authors(session, author_ids)

    for k, v in changes.items():
        setattr(book, k, v)

    await session.flush()
    # Refresh the trigger-updated `updated_at` along with relationships, otherwise the
    # ORM holds a stale value (or worse, marks it expired → lazy load → MissingGreenlet).
    await session.refresh(book, attribute_names=["authors", "genre", "updated_at"])
    return book


async def delete_book(session: AsyncSession, book_id: UUID) -> None:
    book = await session.get(Book, book_id)
    if not book:
        raise NotFoundError("Book", book_id)
    await session.delete(book)
    await session.flush()


async def similar_books_stmt(session: AsyncSession, book_id: UUID) -> Select:
    """Build the SELECT for books similar to the given one. Caller paginates.

    Scoring: +3 shared author, +2 same genre, +1 within ±5 years. Source excluded.

    A pre-filter restricts the scanned set to books that share at least one signal with
    the source — without it, the DB scored every row in `books` even when only a handful
    were candidates. With it, scoring runs on the union of (any-shared-author OR same-genre
    OR nearby-year), which is bounded by what the indexes can find.
    """
    src = await session.get(Book, book_id, options=[selectinload(Book.authors)])
    if not src:
        raise NotFoundError("Book", book_id)

    src_author_ids = [a.id for a in src.authors]

    shared_authors_count = (
        select(func.count())
        .select_from(book_authors)
        .where(
            book_authors.c.book_id == Book.id,
            book_authors.c.author_id.in_(src_author_ids) if src_author_ids else False,
        )
        .correlate(Book)
        .scalar_subquery()
    )

    score = (
        case((shared_authors_count > 0, 3), else_=0)
        + (case((Book.genre_id == src.genre_id, 2), else_=0) if src.genre_id else 0)
        + (
            case((func.abs(Book.published_year - src.published_year) <= 5, 1), else_=0)
            if src.published_year is not None
            else 0
        )
    ).label("score")

    candidates = []
    if src_author_ids:
        candidates.append(
            Book.id.in_(
                select(book_authors.c.book_id).where(book_authors.c.author_id.in_(src_author_ids))
            )
        )
    if src.genre_id:
        candidates.append(Book.genre_id == src.genre_id)
    if src.published_year is not None:
        candidates.append(func.abs(Book.published_year - src.published_year) <= 5)

    stmt = (
        select(Book, score)
        .where(Book.id != book_id)
        .options(selectinload(Book.authors))
        .order_by(score.desc(), Book.created_at.desc(), Book.id)
    )
    if candidates:
        stmt = stmt.where(or_(*candidates))
    else:
        stmt = stmt.where(False)
    return stmt


async def iter_all_books(session: AsyncSession, filters: BookFilters):
    """Streaming-friendly iteration for export."""
    stmt = (
        apply_filters(select(Book), filters)
        .options(selectinload(Book.authors))
        .order_by(Book.created_at, Book.id)
        .execution_options(yield_per=500)
    )
    result = await session.stream(stmt)
    async for partition in result.scalars().partitions(500):
        for book in partition:
            yield book
