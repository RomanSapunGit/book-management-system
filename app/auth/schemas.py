from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints

# Argon2 has no built-in hard cap, but we reject unreasonably long passwords to prevent DoS
# via slow-hashing a 1 GiB string. 128 bytes is plenty of entropy.
Password = Annotated[str, StringConstraints(min_length=8, max_length=128)]


class RegisterRequest(BaseModel):
    email: EmailStr
    password: Password


class LoginRequest(BaseModel):
    email: EmailStr
    password: Password


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # access-token TTL in seconds; clients use this to schedule refreshes.


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    created_at: datetime
