"""Bulk import implementation.

Drives both the parsing (CSV/JSON → row dicts) and the per-row insert with savepoints.

Atomicity model: one row = one savepoint. A row's INSERT either commits within the outer
transaction or rolls back to its savepoint, leaving its neighbors untouched. This is what
"atomic per record" buys you: a row's FK violation, unique violation, or validation error
does not poison the rest of the batch.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authors import service as author_service
from app.books.schemas import BulkImportError, validate_year
from app.config import settings
from app.db.models import Book, Genre, ImportSession
log = logging.getLogger(__name__)


# --- Parsing ---


def _split_names(raw: Any) -> list[str]:
    """Authors column: accept ';' or '|' (commas conflict with CSV)."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw)
    for sep in (";", "|"):
        if sep in s:
            return [p.strip() for p in s.split(sep) if p.strip()]
    return [s.strip()] if s.strip() else []


class _ImportRow(BaseModel):
    """Internal schema for one input row. Resolves authors-by-name + genre-by-name."""

    title: str
    genre: str | None = None
    published_year: int | None = None
    description: str | None = None
    authors: list[str]

    def check_year(self) -> None:
        validate_year(self.published_year)


def _row_to_payload(row: dict[str, Any]) -> dict[str, Any]:
    py = row.get("published_year")
    if py in (None, ""):
        py_val: Any = None
    else:
        try:
            py_val = int(py)
        except (TypeError, ValueError):
            py_val = py  # let pydantic produce a clean error

    return {
        "title": (row.get("title") or "").strip(),
        "genre": (row.get("genre") or None),
        "published_year": py_val,
        "description": row.get("description") or None,
        "authors": _split_names(row.get("authors"))
        if not isinstance(row.get("authors"), list)
        else row["authors"],
    }


def parse_json_rows(raw: bytes) -> list[dict[str, Any]]:
    """Parse the full payload eagerly. We want top-level parse errors (bad JSON, wrong
    shape) to raise at the call site, not lazily during iteration — that's how the route's
    `except ValueError` actually catches them. The 2 MiB cap on the request body keeps the
    materialized list small."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"Invalid JSON: {e}") from e
    if isinstance(data, dict) and "books" in data:
        data = data["books"]
    if not isinstance(data, list):
        raise ValueError("JSON payload must be a list of books or {books: [...]}")
    return data


def parse_csv_rows(raw: bytes) -> list[dict[str, Any]]:
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or "title" not in reader.fieldnames:
        raise ValueError("CSV must have a header row including at least 'title'")
    return list(reader)


# --- Per-row import ---


async def run_import(
    session: AsyncSession, *, session_record: ImportSession, rows: list[dict[str, Any]]
) -> ImportSession:
    """Drive the import against an existing ImportSession row.

    The session_record is updated in place with running counts and final status. The caller
    owns committing the outer transaction.
    """
    session_record.status = "running"
    session_record.started_at = datetime.now(timezone.utc)
    await session.flush()

    errors: list[dict] = []
    error_count_total = 0
    successful = 0
    received = 0

    for idx, raw in enumerate(rows, start=1):
        received += 1

        # --- Per-row atomicity boundary ---
        try:
            async with session.begin_nested():  # SAVEPOINT
                await _apply_row(session, idx, raw)
            successful += 1
        except _RowError as e:
            error_count_total += 1
            if len(errors) < settings.bulk_import_max_stored_errors:
                errors.append({"row": idx, "reason": str(e)})
        except Exception as e:
            # Unexpected failure — log it so we can investigate, still record the row as failed.
            log.exception("unexpected row failure", extra={"row": idx})
            error_count_total += 1
            if len(errors) < settings.bulk_import_max_stored_errors:
                errors.append({"row": idx, "reason": f"unexpected: {type(e).__name__}"})

    session_record.received = received
    session_record.successful = successful
    session_record.failed = error_count_total
    session_record.errors = errors
    session_record.error_count_total = error_count_total
    session_record.status = "completed"
    session_record.completed_at = datetime.now(timezone.utc)
    await session.flush()
    return session_record


class _RowError(Exception):
    """Anticipated per-row failure (validation, unknown genre, etc.). Reported in the result.

    Distinct from unexpected exceptions, which we log loudly even when the row report is the
    same shape — easier to spot a systemic problem in logs vs an operator's bad CSV.
    """


async def _apply_row(session: AsyncSession, row_no: int, raw: dict) -> None:
    try:
        row = _ImportRow.model_validate(_row_to_payload(raw))
        row.check_year()
    except ValidationError as e:
        raise _RowError(_compact_validation_error(e)) from e
    except ValueError as e:
        raise _RowError(str(e)) from e

    if not row.title:
        raise _RowError("title must not be empty")
    if not row.authors:
        raise _RowError("at least one author required")

    genre: Genre | None = None
    if row.genre:
        key = row.genre.strip().casefold()
        genre = (
            await session.execute(
                select(Genre).where((func.lower(Genre.name) == key) | (Genre.slug == key))
            )
        ).scalar_one_or_none()
        if genre is None:
            raise _RowError(f"unknown genre {row.genre!r}")

    authors = await author_service.create_authors_from_names(session, row.authors)

    session.add(
        Book(
            title=row.title,
            genre_id=genre.id if genre else None,
            published_year=row.published_year,
            description=row.description,
            authors=authors,
        )
    )
    await session.flush()  # surface integrity errors NOW so the savepoint rolls back this row.


def _compact_validation_error(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", []))
        parts.append(f"{loc}: {err.get('msg')}")
    return "; ".join(parts)[:500]


# --- Streaming export (kept; unrelated to the import refactor) ---


async def stream_books_csv(book_iter: AsyncIterator) -> AsyncIterator[bytes]:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "title", "genre", "published_year", "authors", "description"])
    yield buf.getvalue().encode("utf-8")
    buf.seek(0)
    buf.truncate(0)

    async for book in book_iter:
        writer.writerow(
            [
                str(book.id),
                book.title,
                book.genre.name if book.genre else "",
                book.published_year if book.published_year is not None else "",
                "; ".join(a.name for a in book.authors),
                (book.description or "").replace("\n", " "),
            ]
        )
        yield buf.getvalue().encode("utf-8")
        buf.seek(0)
        buf.truncate(0)


async def stream_books_json(book_iter: AsyncIterator) -> AsyncIterator[bytes]:
    yield b'{"books":['
    first = True
    async for book in book_iter:
        prefix = b"" if first else b","
        first = False
        obj = {
            "id": str(book.id),
            "title": book.title,
            "genre": book.genre.name if book.genre else None,
            "published_year": book.published_year,
            "description": book.description,
            "authors": [{"id": str(a.id), "name": a.name} for a in book.authors],
        }
        yield prefix + json.dumps(obj, ensure_ascii=False).encode("utf-8")
    yield b"]}"
