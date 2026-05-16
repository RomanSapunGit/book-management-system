"""Rate limiting for the bulk-import endpoint.

DB-backed: counts ImportSession rows for the calling user in the last hour. Each attempt
inserts a row (even on failure), so this is the natural counter without bringing in Redis.

Trade-off: one extra indexed SELECT per /books/import call. Acceptable because the endpoint
is low-frequency by design. Cluster-wide accuracy: holds (DB is shared). Survives restart:
yes.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user_id
from app.config import settings
from app.db import get_db_session
from app.db.models import ImportSession


async def enforce_import_rate_limit(
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db_session),
) -> UUID:
    cutoff = datetime.now(UTC) - settings.import_rate_window
    count = await db.scalar(
        select(func.count())
        .select_from(ImportSession)
        .where(ImportSession.user_id == user_id, ImportSession.created_at >= cutoff)
    )
    if count is not None and count >= settings.import_rate_limit_per_window:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Import rate limit exceeded ({settings.import_rate_limit_per_window} "
                f"per {int(settings.import_rate_window.total_seconds() / 3600)} hour(s)). "
                f"Try again later."
            ),
            headers={"Retry-After": str(int(settings.import_rate_window.total_seconds()))},
        )
    return user_id
