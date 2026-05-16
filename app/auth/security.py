"""Password hashing, JWT encode/decode, and refresh-token generation.

Pure functions — no DB, no IO. Easy to unit-test.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.config import settings

# Argon2id, library defaults — these are sensible 2024+ choices (memory ~64 MiB, t=3, p=4).
# We don't tune lower; the cost is one-time at login, not per-request.
_hasher = PasswordHasher()


def hash_password(plaintext: str) -> str:
    return _hasher.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    try:
        _hasher.verify(hashed, plaintext)
        return True
    except VerifyMismatchError:
        return False
    except Exception:
        # A malformed stored hash (e.g. legacy bcrypt during a migration) should not 500;
        # treat as a mismatch so the auth response is uniform.
        return False


# --- JWT ---


class TokenError(Exception):
    """Raised when an access token is invalid, expired, or malformed."""


def encode_access_token(user_id: UUID) -> tuple[str, int]:
    """Returns (token, ttl_seconds). TTL is returned so the route can echo it to the client."""
    ttl_seconds = settings.access_token_ttl_minutes * 60
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "type": "access",
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, ttl_seconds


def decode_access_token(token: str) -> UUID:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as e:
        raise TokenError("token expired") from e
    except jwt.InvalidTokenError as e:
        raise TokenError("invalid token") from e
    if payload.get("type") != "access":
        # Don't let a refresh token (or anything else with a `sub`) be used as an access token.
        raise TokenError("wrong token type")
    sub = payload.get("sub")
    if not sub:
        raise TokenError("missing sub")
    try:
        return UUID(sub)
    except ValueError as e:
        raise TokenError("invalid sub") from e


# --- Refresh tokens ---


def generate_refresh_token() -> str:
    """A 256-bit url-safe random string. Stored only as sha256 in the DB."""
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
