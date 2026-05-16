from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

GenreName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class GenreRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str


class GenreCreate(BaseModel):
    name: GenreName


class GenreUpdate(BaseModel):
    """Strict partial. Only `name` is mutable; `slug` is always derived from `name` to
    avoid drift between the two unique columns."""

    model_config = ConfigDict(extra="forbid")

    name: GenreName | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if self.name is None:
            raise ValueError("No fields provided to update")
        return self
