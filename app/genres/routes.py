from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi_pagination import LimitOffsetPage
from fastapi_pagination.ext.sqlalchemy import apaginate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user_id
from app.db import get_db_session
from app.db.models import Genre
from app.genres import service as genre_service
from app.genres.schemas import GenreCreate, GenreRead, GenreUpdate

router = APIRouter()


@router.get("", response_model=LimitOffsetPage[GenreRead])
async def list_genres(db: AsyncSession = Depends(get_db_session)):
    return await apaginate(db, select(Genre).order_by(Genre.name))


@router.post(
    "",
    response_model=GenreRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_user_id)],
)
async def create_genre(payload: GenreCreate, db: AsyncSession = Depends(get_db_session)):
    """Strict create. 409 on case-insensitive name *or* slug duplicate (the slug is derived
    from the name, so distinct-looking names can collide)."""
    return await genre_service.create_genre(db, name=payload.name)


@router.get("/{genre_id}", response_model=GenreRead)
async def get_genre(genre_id: UUID, db: AsyncSession = Depends(get_db_session)):
    return await genre_service.get_genre(db, genre_id)


@router.patch(
    "/{genre_id}",
    response_model=GenreRead,
    dependencies=[Depends(get_current_user_id)],
)
async def update_genre(
    genre_id: UUID, payload: GenreUpdate, db: AsyncSession = Depends(get_db_session)
):
    return await genre_service.update_genre(db, genre_id, name=payload.name)


@router.delete(
    "/{genre_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(get_current_user_id)],
)
async def delete_genre(genre_id: UUID, db: AsyncSession = Depends(get_db_session)):
    """Hard delete. 409 if any book still references this genre (FK RESTRICT)."""
    await genre_service.delete_genre(db, genre_id)
    return None
