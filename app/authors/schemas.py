from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

ShortStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class AuthorCreate(BaseModel):
    name: ShortStr
    bio: str | None = None


class AuthorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    bio: str | None
    created_at: datetime
    updated_at: datetime
