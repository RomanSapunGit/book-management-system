"""
Tests run against a real Postgres (env: `TEST_DATABASE_URL`). They are skipped automatically
if no Postgres is reachable.

Mocked DBs were a deliberate non-choice. The most important contracts in this service —
the case-insensitive email unique index, the `published_year` CHECK, the genre
FK with RESTRICT, the M2M cascade behavior — live in Postgres. A mock that passes while those
are wrong is worse than no test.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# TEST_DATABASE_URL wins over DATABASE_URL — the api container has DATABASE_URL pointing at
# the prod `books` DB, and running `docker exec api pytest` must not nuke it.
if "TEST_DATABASE_URL" in os.environ:
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
else:
    os.environ.setdefault(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/books_test",
    )
# 32+ bytes to satisfy RFC 7518 §3.2 — silences PyJWT's InsecureKeyLengthWarning under HS256.
os.environ.setdefault("JWT_SECRET", "test-secret-do-not-use-in-prod-xxxx")
os.environ.setdefault("LOG_FORMAT", "plain")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.db import get_session_factory
from app.db.models import Base
from app.main import app

# Genres seeded by the migration; tests use them by name.
SEED_GENRES = [
    ("Fiction", "fiction"),
    ("Non-Fiction", "non-fiction"),
    ("Fantasy", "fantasy"),
    ("Science Fiction", "science-fiction"),
    ("Mystery", "mystery"),
    ("Thriller", "thriller"),
    ("Romance", "romance"),
    ("Horror", "horror"),
    ("Biography", "biography"),
    ("History", "history"),
    ("Self-Help", "self-help"),
    ("Children", "children"),
    ("Young Adult", "young-adult"),
    ("Poetry", "poetry"),
    ("Drama", "drama"),
]


def _pg_available() -> bool:
    import socket

    url = settings.normalized_database_url
    try:
        host_port = url.split("@", 1)[1].split("/", 1)[0]
        host, port = host_port.split(":")
        with socket.create_connection((host, int(port)), timeout=0.5):
            return True
    except Exception:
        return False


def _ensure_test_database() -> None:
    """Create the target test database if it doesn't exist.

    Lets `pytest` just work against the running `docker compose up -d db` Postgres
    (which only ships the production `books` DB by default). Connecting to the standard
    admin DB `postgres` to issue CREATE DATABASE — that's the conventional path.
    """
    import asyncio

    import asyncpg

    url = settings.normalized_database_url  # postgresql+asyncpg://user:pw@host:port/dbname
    user_pw, host_db = url.split("//", 1)[1].split("@", 1)
    user, password = user_pw.split(":", 1)
    host_port, dbname = host_db.split("/", 1)
    host, port = host_port.split(":")

    async def _go():
        admin = await asyncpg.connect(
            user=user, password=password, host=host, port=int(port), database="postgres"
        )
        try:
            exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", dbname)
            if not exists:
                # asyncpg doesn't allow parameter binding for DDL; dbname comes from our own
                # settings, not from any request, so quoting via format() is safe here.
                await admin.execute(f'CREATE DATABASE "{dbname}"')
        finally:
            await admin.close()

    import contextlib

    with contextlib.suppress(Exception):
        # If we can't create the DB (permissions, etc.), the engine fixture will surface
        # the real error. Don't mask it here.
        asyncio.run(_go())


pg_available = _pg_available()
if pg_available:
    _ensure_test_database()


@pytest_asyncio.fixture(scope="session")
async def _engine():
    """Build the schema once per session. We mirror what migrations do (functional indexes,
    seed genres) so the test DB matches production behavior. When Postgres is unreachable
    this yields None — the per-test fixture below short-circuits in that case."""
    if not pg_available:
        yield None
        return
    engine = create_async_engine(settings.normalized_database_url)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower ON users (lower(email))")
        )
        await conn.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS uq_genres_name_lower ON genres (lower(name))")
        )
        await conn.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS uq_genres_slug_lower ON genres (lower(slug))")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_books_title_lower ON books (lower(title))")
        )
        # The model uses python-side `default=uuid4` for `id` (works through the ORM); the
        # migration adds `server_default=gen_random_uuid()`. We're not running migrations
        # here (create_all from metadata), so we provide the id explicitly.
        import uuid as _uuid

        for name, slug in SEED_GENRES:
            await conn.execute(
                text(
                    "INSERT INTO genres (id, name, slug) VALUES (:id, :n, :s) ON CONFLICT DO NOTHING"
                ),
                {"id": _uuid.uuid4(), "n": name, "s": slug},
            )
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(_engine):
    """Reset mutable tables between tests. Genres are now mutable too (CRUD endpoints),
    so we truncate and re-seed the default set — tests that look up "Fantasy" by name
    still find it, but a genre a previous test created is gone."""
    if _engine is None:
        yield
        return
    import uuid as _uuid

    async with _engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE import_sessions, refresh_tokens, users, book_authors, books, "
                "authors, genres RESTART IDENTITY CASCADE"
            )
        )
        for name, slug in SEED_GENRES:
            await conn.execute(
                text("INSERT INTO genres (id, name, slug) VALUES (:id, :n, :s)"),
                {"id": _uuid.uuid4(), "n": name, "s": slug},
            )
    yield


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    get_session_factory()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def auth_headers(client: AsyncClient) -> dict[str, str]:
    """A registered+logged-in user. Tests that need to call mutating endpoints use this."""
    await client.post(
        "/auth/register", json={"email": "alice@example.com", "password": "correct-horse-battery"}
    )
    r = await client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "correct-horse-battery"}
    )
    body = r.json()
    return {"Authorization": f"Bearer {body['access_token']}"}
