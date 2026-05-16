from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Author
from app.exceptions import NotFoundError


async def create_author(session: AsyncSession, *, name: str, bio: str | None) -> Author:
    """Create an author. Duplicates (same name, same case, even same person) are allowed —
    two real authors can share a name, and the system doesn't model identity beyond an id."""
    author = Author(name=name.strip(), bio=bio)
    session.add(author)
    await session.flush()
    return author


async def create_authors_from_names(
    session: AsyncSession, names: Iterable[str]
) -> list[Author]:
    """Bulk-create one Author row per input name. No deduplication: a name appearing twice
    in `names` produces two distinct Author rows, mirroring how a CSV row with repeated
    authors honestly cannot prove they're the same person."""
    created: list[Author] = []
    for raw in names:
        n = (raw or "").strip()
        if not n:
            continue
        author = Author(name=n)
        session.add(author)
        created.append(author)
    if created:
        await session.flush()
    return created


async def get_authors_by_ids(session: AsyncSession, ids: Iterable[UUID]) -> list[Author]:
    ids = list(set(ids))
    if not ids:
        return []
    rows = (await session.execute(select(Author).where(Author.id.in_(ids)))).scalars().all()
    if len(rows) != len(ids):
        found = {a.id for a in rows}
        missing = [i for i in ids if i not in found]
        raise NotFoundError("Author", missing[0])
    return rows
