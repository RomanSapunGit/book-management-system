from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import security
from app.auth.schemas import TokenPair
from app.config import settings
from app.db.models import RefreshToken, User
from app.exceptions import ConflictError, UnauthorizedError

log = logging.getLogger(__name__)


async def register_user(session: AsyncSession, *, email: str, password: str) -> User:
    email_norm = email.strip().casefold()
    existing = (
        await session.execute(select(User).where(func.lower(User.email) == email_norm))
    ).scalar_one_or_none()
    if existing:
        raise ConflictError("Email already registered")
    user = User(email=email_norm, password_hash=security.hash_password(password))
    session.add(user)
    await session.flush()
    return user


async def authenticate(session: AsyncSession, *, email: str, password: str) -> User:
    email_norm = email.strip().casefold()
    user = (
        await session.execute(select(User).where(func.lower(User.email) == email_norm))
    ).scalar_one_or_none()
    if user is None:
        security.verify_password(password, _DUMMY_HASH)
        raise UnauthorizedError("Invalid credentials")
    if not security.verify_password(password, user.password_hash):
        raise UnauthorizedError("Invalid credentials")
    return user


# Pre-computed once at import time — used to equalize timing for the "user not found" path.
_DUMMY_HASH = security.hash_password(settings.auth_dummy_password)


async def issue_tokens(session: AsyncSession, user_id: UUID) -> TokenPair:
    access_token, ttl_seconds = security.encode_access_token(user_id)
    refresh_raw = security.generate_refresh_token()
    record = RefreshToken(
        user_id=user_id,
        token_hash=security.hash_refresh_token(refresh_raw),
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days),
    )
    session.add(record)
    await session.flush()
    return TokenPair(access_token=access_token, refresh_token=refresh_raw, expires_in=ttl_seconds)


async def refresh_tokens(session: AsyncSession, *, presented: str) -> TokenPair:
    """Validate a refresh token, rotate it, and return a new pair.

    Reuse detection: if the presented token was already rotated (revoked with a `replaced_by_id`),
    that means somebody other than the legitimate client just used a token we issued previously.
    The textbook response is to assume theft and revoke every refresh token for that user.
    """
    token_hash = security.hash_refresh_token(presented)
    row = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    ).scalar_one_or_none()

    if row is None:
        raise UnauthorizedError("Invalid refresh token")

    now = datetime.now(UTC)
    expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    if expires_at < now:
        raise UnauthorizedError("Refresh token expired")

    if row.revoked_at is not None:
        # Two reasons a token is revoked:
        #   - it was rotated (`replaced_by_id` set) → presenting it again means SOMEONE has a
        #     copy they shouldn't have. Assume theft and revoke every session for the user.
        #   - it was explicitly logged out (`replaced_by_id` null) → boring; just refuse.
        if row.replaced_by_id is not None:
            log.warning(
                "refresh-token reuse detected user_id=%s — revoking all sessions", row.user_id
            )
            await revoke_all_for_user(session, row.user_id)
            await session.commit()  # persist the revocation BEFORE the rollback-on-raise.
            raise UnauthorizedError("Refresh token reuse detected; all sessions revoked")
        raise UnauthorizedError("Refresh token revoked")

    # Issue the replacement first so we can record its id on the old row.
    new_pair = await issue_tokens(session, row.user_id)
    new_row = (
        await session.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == security.hash_refresh_token(new_pair.refresh_token)
            )
        )
    ).scalar_one()
    row.revoked_at = now
    row.replaced_by_id = new_row.id
    await session.flush()
    return new_pair


async def revoke(session: AsyncSession, *, presented: str) -> None:
    """Idempotent: revoking an unknown / already-revoked token is a no-op (returns 204 either way)."""
    token_hash = security.hash_refresh_token(presented)
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == token_hash, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def revoke_all_for_user(session: AsyncSession, user_id: UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
