from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from fastapi_pagination import LimitOffsetPage
from fastapi_pagination.ext.sqlalchemy import apaginate
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user_id
from app.authors import service as author_service
from app.authors.schemas import AuthorCreate, AuthorRead
from app.db import get_db_session
from app.db.models import Author
from app.exceptions import NotFoundError

router = APIRouter()


@router.post(
    "",
    response_model=AuthorRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_user_id)],
)
async def create_author(payload: AuthorCreate, db: AsyncSession = Depends(get_db_session)):
    """Create an author. Duplicate names are allowed — two real people can share a name,
    and disambiguation is delegated to clients (via id). Use `GET /authors?q=...` to find
    existing rows before creating a new one if you want to avoid duplicates."""
    return await author_service.create_author(db, name=payload.name, bio=payload.bio)


@router.get("", response_model=LimitOffsetPage[AuthorRead])
async def list_authors(
    db: AsyncSession = Depends(get_db_session),
    q: str | None = Query(default=None, description="Case-insensitive name substring"),
):
    stmt = select(Author)
    if q:
        stmt = stmt.where(func.lower(Author.name).contains(q.casefold()))
    stmt = stmt.order_by(Author.name, Author.id)
    return await apaginate(db, stmt)


@router.get("/{author_id}", response_model=AuthorRead)
async def get_author(author_id: UUID, db: AsyncSession = Depends(get_db_session)):
    author = await db.get(Author, author_id)
    if not author:
        raise NotFoundError("Author", author_id)
    return author
