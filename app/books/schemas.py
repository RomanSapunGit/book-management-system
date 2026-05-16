from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.authors.schemas import AuthorRead
from app.genres.schemas import GenreRead

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


def _current_year() -> int:
    return datetime.now(UTC).year


def validate_year(v: int | None) -> int | None:
    if v is None:
        return None
    if v < 1800:
        raise ValueError("published_year must be >= 1800")
    if v > _current_year():
        raise ValueError(f"published_year must be <= {_current_year()} (the current year)")
    return v


class BookBase(BaseModel):
    title: NonEmptyStr
    genre_id: UUID | None = None
    published_year: int | None = None
    description: str | None = None

    @field_validator("published_year")
    @classmethod
    def _year(cls, v):
        return validate_year(v)


class BookCreate(BookBase):
    author_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def _at_least_one_author(self):
        if not self.author_ids:
            raise ValueError("At least one author_id is required")
        # The DB UNIQUE+JoinTable doesn't enforce dedupe inside a single request; we do here.
        if len(set(self.author_ids)) != len(self.author_ids):
            raise ValueError("author_ids must not contain duplicates")
        return self


class BookUpdate(BaseModel):
    """PATCH semantics. Only provided fields are touched; `null` clears nullable columns."""

    model_config = ConfigDict(extra="forbid")

    title: NonEmptyStr | None = None
    genre_id: UUID | None = None
    published_year: int | None = None
    description: str | None = None
    author_ids: list[UUID] | None = None

    @field_validator("published_year")
    @classmethod
    def _year(cls, v):
        return validate_year(v)


class BookRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    genre: GenreRead | None
    published_year: int | None
    description: str | None
    authors: list[AuthorRead]
    created_at: datetime
    updated_at: datetime


# --- Bulk import (POST /books/import + GET /books/import) ---


class BulkImportError(BaseModel):
    row: int
    reason: str


class ImportSessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str  # 'pending' | 'running' | 'completed' | 'failed'
    filename: str | None
    format: str
    received: int
    successful: int
    failed: int
    errors: list[BulkImportError]
    # When the writer truncates the `errors` list at the cap (1000), this counts ALL failures
    # so the client can render "1000 shown, 4523 total errors".
    error_count_total: int
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class SimilarBookRead(BookRead):
    """A book + the similarity score used to rank it. Exposed so the client can render
    relevance ("matches author + genre")."""
    score: int
