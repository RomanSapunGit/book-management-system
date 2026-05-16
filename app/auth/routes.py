from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service as auth_service
from app.auth.deps import get_current_user_id
from app.auth.schemas import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserRead,
)
from app.db import get_db_session

router = APIRouter()


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db_session)):
    user = await auth_service.register_user(db, email=payload.email, password=payload.password)
    return user


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db_session)):
    user = await auth_service.authenticate(db, email=payload.email, password=payload.password)
    return await auth_service.issue_tokens(db, user.id)


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db_session)):
    return await auth_service.refresh_tokens(db, presented=payload.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: LogoutRequest, db: AsyncSession = Depends(get_db_session)):
    """Revoke the supplied refresh token (single device). Idempotent."""
    await auth_service.revoke(db, presented=payload.refresh_token)
    return None


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    db: AsyncSession = Depends(get_db_session),
    user_id: UUID = Depends(get_current_user_id),
):
    """Revoke every refresh token for the authenticated user — 'lost my phone' button."""
    await auth_service.revoke_all_for_user(db, user_id)
    return None
