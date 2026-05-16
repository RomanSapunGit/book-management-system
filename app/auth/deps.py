from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Header

from app.auth.security import TokenError, decode_access_token
from app.exceptions import UnauthorizedError


async def get_current_user_id(
    authorization: str | None = Header(default=None),
) -> UUID:
    """Extract user id from the Bearer access token.

    Intentionally does NOT touch the DB on the hot path. The token signature + expiry give us
    enough authority for mutating endpoints; revocation only applies to refresh tokens (which
    are checked against the DB in the /auth/refresh handler). Short access TTLs (15 min) make
    the revocation gap acceptable.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("Missing or malformed Authorization header")
    token = authorization[7:].strip()
    try:
        return decode_access_token(token)
    except TokenError as e:
        raise UnauthorizedError(str(e)) from e


CurrentUserId = Depends(get_current_user_id)
