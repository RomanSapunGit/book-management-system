from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from fastapi_pagination import LimitOffsetPage
from fastapi_pagination.ext.sqlalchemy import apaginate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user_id
from app.books import bulk as bulk_module
from app.books import service as book_service
from app.books.import_deps import enforce_import_rate_limit
from app.books.schemas import (
    BookCreate,
    BookRead,
    BookUpdate,
    ImportSessionRead,
    SimilarBookRead,
)
from app.books.service import BookFilters
from app.db import get_db_session
from app.db.models import ImportSession

log = logging.getLogger(__name__)
router = APIRouter()


# --- Books CRUD ---


@router.post(
    "",
    response_model=BookRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_user_id)],
)
async def create_book(payload: BookCreate, db: AsyncSession = Depends(get_db_session)):
    return await book_service.create_book(
        db,
        title=payload.title,
        genre_id=payload.genre_id,
        published_year=payload.published_year,
        description=payload.description,
        author_ids=payload.author_ids,
    )


@router.get("", response_model=LimitOffsetPage[BookRead])
async def list_books(
    db: AsyncSession = Depends(get_db_session),
    title: str | None = Query(default=None, max_length=500),
    author: str | None = Query(default=None, max_length=255),
    author_id: UUID | None = Query(default=None),
    genre: str | None = Query(default=None, max_length=100),
    genre_id: UUID | None = Query(default=None),
    year_from: int | None = Query(default=None, ge=1800),
    year_to: int | None = Query(default=None, ge=1800),
    sort_by: Literal["title", "published_year", "created_at", "updated_at"] = "created_at",
    sort_dir: Literal["asc", "desc"] = "desc",
):
    if year_from is not None and year_to is not None and year_from > year_to:
        raise HTTPException(status_code=422, detail="year_from must be <= year_to")
    filters = BookFilters(
        title=title,
        author=author,
        author_id=author_id,
        genre=genre,
        genre_id=genre_id,
        year_from=year_from,
        year_to=year_to,
    )
    stmt = book_service.build_list_stmt(filters, sort_by=sort_by, sort_dir=sort_dir)
    return await apaginate(db, stmt)


# --- Export ---


@router.get("/export")
async def export_books(
    db: AsyncSession = Depends(get_db_session),
    format: Literal["json", "csv"] = Query(default="json"),
    title: str | None = None,
    author: str | None = None,
    author_id: UUID | None = None,
    genre: str | None = None,
    genre_id: UUID | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
):
    filters = BookFilters(
        title=title,
        author=author,
        author_id=author_id,
        genre=genre,
        genre_id=genre_id,
        year_from=year_from,
        year_to=year_to,
    )
    book_iter = book_service.iter_all_books(db, filters)
    if format == "csv":
        return StreamingResponse(
            bulk_module.stream_books_csv(book_iter),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="books.csv"'},
        )
    return StreamingResponse(
        bulk_module.stream_books_json(book_iter),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="books.json"'},
    )


# --- Bulk import ---


@router.post(
    "/import",
    response_model=ImportSessionRead,
    status_code=status.HTTP_200_OK,
)
async def import_books(
    file: UploadFile = File(...),
    user_id: UUID = Depends(enforce_import_rate_limit),  # auth + rate limit in one dep chain
    db: AsyncSession = Depends(get_db_session),
):
    """Bulk-create books from CSV or JSON.

    Size limiting lives in `MaxBodySizeMiddleware` (streams `receive`, aborts at the cap)
    plus nginx `client_max_body_size` in production. By the time this handler runs the
    body is guaranteed to be under `settings.bulk_import_max_bytes`.

    Other constraints:
      - Auth: required.
      - Rate limit: 5 imports per user per hour.
      - Atomicity: per-row savepoints — one bad row does not affect its neighbors.
      - Persistence: every attempt creates an `import_sessions` row, visible to the caller
        via `GET /books/import` (audit list).
    """
    ctype = (file.content_type or "").lower()
    fname = (file.filename or "").lower()
    is_csv = ctype in ("text/csv", "application/csv") or fname.endswith(".csv")
    is_json = ctype in ("application/json", "text/json") or fname.endswith(".json")
    if not (is_csv or is_json):
        raise HTTPException(status_code=415, detail="Use CSV or JSON (Content-Type or filename)")
    fmt = "csv" if is_csv else "json"

    raw = await file.read()

    # Create the session row up front so it exists even if parsing immediately fails.
    # This also makes the rate-limit count honest — a failed attempt still counts.
    session_record = ImportSession(
        user_id=user_id,
        status="pending",
        filename=file.filename,
        format=fmt,
    )
    db.add(session_record)
    await db.flush()

    try:
        rows = bulk_module.parse_csv_rows(raw) if fmt == "csv" else bulk_module.parse_json_rows(raw)
    except ValueError as e:
        # Top-level parse error — mark the whole session failed and surface it.
        session_record.status = "failed"
        session_record.errors = [{"row": 0, "reason": str(e)}]
        session_record.error_count_total = 1
        await db.flush()
        raise HTTPException(status_code=400, detail=str(e)) from e

    await bulk_module.run_import(db, session_record=session_record, rows=rows)
    return session_record


@router.get("/import", response_model=LimitOffsetPage[ImportSessionRead])
async def list_import_sessions(
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db_session),
):
    """Audit list of the caller's own import attempts, newest first. Strictly scoped to
    `user_id` — one user cannot see another's import history (or even existence)."""
    stmt = (
        select(ImportSession)
        .where(ImportSession.user_id == user_id)
        .order_by(ImportSession.created_at.desc(), ImportSession.id.desc())
    )
    return await apaginate(db, stmt)


# --- Recommendations ---


@router.get("/{book_id}/similar", response_model=LimitOffsetPage[SimilarBookRead])
async def list_similar(
    book_id: UUID,
    db: AsyncSession = Depends(get_db_session),
):
    """Books similar to the given one. Deterministic scoring on shared authors, same genre,
    and publication-year proximity. No ML, no personalization."""
    stmt = await book_service.similar_books_stmt(db, book_id)
    return await apaginate(
        db,
        stmt,
        transformer=lambda rows: [
            SimilarBookRead(
                **BookRead.model_validate(book, from_attributes=True).model_dump(), score=int(score)
            )
            for book, score in rows
        ],
    )


# --- Single-book CRUD (define AFTER /export, /import, /{id}/similar so prefix-matching wins) ---


@router.get("/{book_id}", response_model=BookRead)
async def get_book(book_id: UUID, db: AsyncSession = Depends(get_db_session)):
    return await book_service.get_book(db, book_id)


@router.patch(
    "/{book_id}",
    response_model=BookRead,
    dependencies=[Depends(get_current_user_id)],
)
async def update_book(
    book_id: UUID, payload: BookUpdate, db: AsyncSession = Depends(get_db_session)
):
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No fields provided to update")
    return await book_service.update_book(db, book_id, changes=changes)


@router.delete(
    "/{book_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(get_current_user_id)],
)
async def delete_book(book_id: UUID, db: AsyncSession = Depends(get_db_session)):
    await book_service.delete_book(db, book_id)
    return None
