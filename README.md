# Book Management API

Async FastAPI service for managing books and authors, backed by PostgreSQL, with JWT-based
auth (access + revocable refresh) and a CLI seeder.

This README is structured around the **decisions** — what's enforced, where, and what's
deliberately out of scope. CRUD is uninteresting; the judgment is in the rest.

---

## Quick start

```bash
cp .env.example .env
# Generate a real JWT secret before doing anything other than local dev:
# python -c "import secrets; print(secrets.token_urlsafe(64))"

docker compose up --build
# Swagger UI:  http://localhost:8000/docs       — interactive, "Try it out"
# ReDoc:       http://localhost:8000/redoc      — read-only, hand to consumers
# OpenAPI:     http://localhost:8000/openapi.json
# Health:      http://localhost:8000/health
```

Migrations run automatically on container boot. To run against a local DB without Docker:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
```

Run the tests (real Postgres required — see *Testing*):

```bash
docker run -d --name books-test-pg \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres -e POSTGRES_DB=books_test \
  -p 5432:5432 postgres:16-alpine
pytest
```

Seed with example data:

```bash
python -m app.seed examples/books.json
python -m app.seed examples/books.csv --format csv
```

---

## Endpoints

### Public (no auth)

| Method | Path | Notes |
|---|---|---|
| `GET` | `/books` | Filter by `title`, `author`, `author_id`, `genre`, `genre_id`, `year_from`, `year_to`. Sort + paginate. `X-Total-Count` header. |
| `GET` | `/books/{id}` | |
| `GET` | `/books/export` | Stream all matching books as JSON or CSV. |
| `GET` | `/authors` | Substring search by `q`. |
| `GET` | `/authors/{id}` | |
| `GET` | `/genres` | The predefined list, seeded by the migration. |
| `GET` | `/health` | Liveness + DB connectivity (`{status, db}`). |

### Authenticated (Bearer access token)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/books` | Authors referenced by `author_ids` only (must exist). |
| `PATCH` | `/books/{id}` | Strict partial. `null` clears; unknown keys → 422. |
| `DELETE` | `/books/{id}` | |
| `POST` | `/authors` | Strict create — 409 on case-insensitive duplicate. |
| `POST` | `/auth/logout-all` | Revoke every refresh token for the calling user. |

### Auth flow

| Method | Path | Notes |
|---|---|---|
| `POST` | `/auth/register` | `{email, password}` → 201 `{id, email}`. |
| `POST` | `/auth/login` | → `{access_token, refresh_token, expires_in}`. |
| `POST` | `/auth/refresh` | Rotates the refresh token. Reuse of a rotated token revokes *all* sessions. |
| `POST` | `/auth/logout` | Revoke the supplied refresh token. Idempotent. |

---

## Where invariants live, and why

The spec asks explicitly. Here's the rationale.

| Invariant | Pydantic | Service | Database | Why |
|---|---|---|---|---|
| Non-empty title | ✅ | | | Cheap shape check; clean 422 before DB. |
| `published_year >= 1800` | ✅ | | ✅ CHECK | DB CHECK is the immovable floor; if someone hand-INSERTs, they still can't violate it. |
| `published_year <= current_year` | ✅ | | | DB CHECK can't reference `now()` (must be immutable). Pydantic handles it dynamically. |
| Genre must be from predefined list | | | ✅ FK to `genres` | DB-side closed-set membership. Pydantic only validates the *shape* of `genre_id`; the FK is the authority. |
| Author existence | | ✅ `_validate_authors` | ✅ FK | Service returns clean 404 with the offending id; FK is the safety net for races. |
| Case-insensitive author uniqueness | | (cheap pre-check) | ✅ `UNIQUE INDEX (lower(name))` | The DB is the authority — concurrent inserts that bypass the pre-check land as IntegrityError → clean 409. |
| Case-insensitive email uniqueness | | (cheap pre-check) | ✅ `UNIQUE INDEX (lower(email))` | Same shape as authors. |
| At-least-one-author per book | ✅ | ✅ | | Pydantic on create; service rejects empty `author_ids` on PATCH with a clear 409. |
| Sort columns | ✅ whitelist | | | Arbitrary `ORDER BY` is a perf and info-leak footgun. |
| Pagination caps (limit ≤ 200) | ✅ | | | Prevent accidental full-table scans through the public API. |
| Unknown fields on PATCH | ✅ `extra="forbid"` | | | A silently-ignored typo is a bug factory. |

The pattern: **Pydantic for shape and quick rejection, DB for invariants that must hold across
all code paths.** The service layer is for cross-row decisions (existence checks, "did the
client send something coherent?") that don't belong in either place.

---

## Concurrency — two requests try to create the same author at the same time

What happens, in order:

1. Both requests reach `POST /authors`.
2. Both run the cheap pre-check `SELECT WHERE lower(name) = ...` → both see no row.
3. Both attempt `INSERT INTO authors (name) ...`.
4. One INSERT wins; the other hits the unique functional index `uq_authors_name_lower`.
5. SQLAlchemy raises `IntegrityError`. The global handler converts it to a clean `409 Conflict`.

The pre-check is purely an optimization — it produces a friendlier error message in the common
case. The **DB is the authority**. The same shape applies to email uniqueness on `/auth/register`.

For internal bulk operations (the seeder), `INSERT ... ON CONFLICT DO NOTHING` lets the chunk
absorb the race without aborting (`app/authors/service.py:get_or_create_authors_by_name`).

---

## List endpoint with 10k+ books

The relevant indices:

- `ix_books_title_lower` — functional `lower(title)`. The `title` filter is
  `lower(title) LIKE '%x%'`; without this index it's a seqscan.
- `ix_books_genre_id` — filter by genre id (or the join when filtering by name).
- `ix_books_published_year` — year range filters.
- `ix_book_authors_author_id` — the reverse direction of the M2M for "books by author".

The `(genre, title, sort_by=published_year)` path uses the genre index for the WHERE, then
a sort on `published_year`. At 10k rows that's a heap scan + an in-memory sort; well under
50 ms in practice. If we grow to 1M+, a covering composite index
`(genre_id, published_year, id)` becomes worth measuring — not before.

Pagination uses `LIMIT/OFFSET` with `ORDER BY <sort>, id` (id as tiebreaker, deterministic).
Keyset pagination would be cheaper at the tail of huge tables but adds API surface for a
problem we don't have. The "limit ≤ 200" cap puts a ceiling on per-request work either way.

---

## Auth — token lifecycle for long-lived clients

Two-token flow:

- **Access token**: signed JWT, **15 min**, *stateless* (no DB hit per request).
- **Refresh token**: 256 random bits, **30 days**, **stored as `sha256(token)` in
  `refresh_tokens`**. Rotated on every successful `/auth/refresh`; the old row is marked
  `revoked_at` with `replaced_by_id` pointing at its successor.

### Why a DB-backed refresh

If we want real logout / device revocation, *something* has to be stateful. The choices:

- Pure stateless refresh → no revocation. A stolen 30-day token is good for 30 days.
- A JWT denylist → that's a DB table with extra steps.
- A `refresh_tokens` row → cleanest. Touched only on `/auth/refresh` (~every 15 min per active
  session), not on every API request. The cost is one indexed lookup per token-renewal.

### Why argon2 for passwords but sha256 for refresh tokens

Passwords are low-entropy (`hunter2`-class). Slow hashing protects them by making offline
brute-force expensive. Refresh tokens are 256 random bits — there is no "brute force" to slow
down; we just need confidentiality at rest. Argon2 here would be a category error.

### Reuse detection (the property the table really earns)

Refresh tokens rotate on every use. If a token that has *already been rotated* is presented
again, that's a signal someone has a copy they shouldn't — almost always token theft. The
service:

1. Logs a warning with the user id.
2. Revokes every active refresh token for that user.
3. **Commits the revocation before raising** — otherwise the session dependency rolls back
   and the attacker keeps their access. (See `app/auth/service.py:refresh_tokens` — the
   explicit commit is load-bearing, not boilerplate.)
4. Returns 401.

This is distinct from "revoked via logout" (`replaced_by_id IS NULL`). Logging out a token
twice is idempotent and never escalates — only *rotated-then-reused* triggers the global
revoke. Tests cover both paths (`tests/test_auth.py:test_refresh_reuse_revokes_all_sessions`
and `:test_logout_revokes_only_target`).

### Mobile clients

A mobile app keeps its refresh token. When the access token expires (15 min), the client
calls `/auth/refresh` and receives a new pair. No re-login until the refresh itself expires
(30 days) or the user explicitly logs out. Stolen device? Hit `/auth/logout-all` from another
device.

---

## Observability — "which request caused that 500?"

- **Request ID middleware** (`app/middleware.py`): every request gets a UUID — or an
  inbound `X-Request-ID` if a gateway is in front. Echoed back as a response header.
- **Structured JSON logs** (`app/logging_config.py`): every log line carries `request_id`,
  pulled from a `ContextVar` so any code in the request can log without explicitly threading
  it.
- **Error bodies include the request id**: a client seeing `{"detail": "...", "request_id":
  "abc"}` can quote that id to an operator who greps logs for one matching record.
- **One access-log line per request** (method, path, status, ms) emitted by the middleware.
- **`/health` checks DB connectivity** (with a 2s timeout) and returns
  `{status, db: ok|down}` — a probe can distinguish "process up, DB down" from "process
  down" without needing two endpoints.

---

## Bulk import — CLI seeder, not an endpoint

Bulk import is an out-of-band CLI:

```bash
python -m app.seed examples/books.json
python -m app.seed examples/books.csv --format csv
cat books.json | python -m app.seed - --format json
python -m app.seed examples/books.json --force   # allow seeding a non-empty table
```

**Why CLI**, not an HTTP endpoint:

- **Trust boundary.** Bulk import is an operator action, not an end-user action. Putting it
  behind an authenticated upload endpoint blurs that line and adds a 25-MiB attack surface
  (DoS, long-locking transactions, connection exhaustion) that doesn't go away with auth.
- **Operational reality.** Catalog data arrives from publishers as files; an ops person
  runs the seeder. They don't curl through the API.
- **Composability.** A CLI returns a non-zero exit code on failure → cron / Make / CI can
  detect it. A 200 OK with `{failed: 17}` requires every caller to remember to grep the body.

**Semantics**:

- **Insert-only.** Idempotency is the caller's responsibility: by default the seeder refuses
  to run against a non-empty `books` table. Pass `--force` to override.
- **Per-row validation.** One bad row does not abort the batch. Output:
  `received / created / failed` plus per-row errors on stderr. Exit non-zero if anything
  failed.
- **Chunked savepoints** (default 500 rows). A DB-level error in one chunk fails those rows
  but lets the rest commit. The `BULK_IMPORT_MAX_ROWS` env cap (default 50k) prevents a
  50M-row CSV from monopolizing a connection.
- **Author resolution by name.** The seeder uses `INSERT ... ON CONFLICT DO NOTHING` to
  case-insensitively get-or-create authors. This is intentional asymmetry: the **API**
  requires author IDs (clients should know what they reference), but operator imports
  legitimately have raw catalog data with names.

**CSV shape**:

```
title,genre,authors,published_year,description
The Hobbit,Fantasy,J. R. R. Tolkien;Christopher Tolkien,1937,
```

Authors are `;`- or `|`-separated (commas would conflict with CSV). `genre` is the genre's
name or slug (case-insensitive).

**JSON shape**: a top-level list or `{"books": [...]}`. Each book mirrors the CSV columns,
with `authors` as a JSON array.

---

## Schema (owned by `migrations/`, not by `models.py`)

```
genres                authors            books
──────                ───────            ─────
id PK                 id PK              id PK
name UNIQUE           name UNIQUE        title
slug UNIQUE           bio                genre_id  FK→genres   (RESTRICT)
                      created_at         published_year
                      updated_at         description
                                         created_at, updated_at

           book_authors                       users               refresh_tokens
           ────────────                       ─────               ──────────────
           book_id   FK→books   (CASCADE)     id PK               id PK
           author_id FK→authors (RESTRICT)    email UNIQUE        user_id     FK→users (CASCADE)
                                              password_hash       token_hash  UNIQUE
                                                                  expires_at
                                                                  revoked_at
                                                                  replaced_by_id
```

Hand-written initial migration (`0001_initial.py`) — never autogenerated. Notable choices:

- `UNIQUE INDEX uq_authors_name_lower ON authors (lower(name))` — case-insensitive author
  uniqueness at the DB layer.
- `UNIQUE INDEX uq_users_email_lower ON users (lower(email))` — same shape for emails.
- `CHECK (published_year >= 1800)` — the floor lives in the DB; upper bound is Pydantic-only
  because CHECK must be immutable.
- `book_authors.author_id ON DELETE RESTRICT` — refusing to delete an author who still has
  books is the safe default. CASCADE would silently destroy data.
- `books.genre_id ON DELETE RESTRICT` — same shape.
- `refresh_tokens.user_id ON DELETE CASCADE` — when a user is deleted, their tokens go too.
- `updated_at` trigger keeps the column truthful even for raw SQL updates that bypass the
  ORM.
- Genres are seeded by the migration itself. The "predefined list" property only holds if
  adding a genre is a code review event — not an ad-hoc admin INSERT.

### Why M2M (`book_authors`) and not `books.author_id`

Books with multiple authors are common (anthologies, co-authored works). A single FK forces
a lie ("primary author"). The M2M cost is one join table; the simplification cost of `1:N`
is permanent data fidelity loss.

### Why ORM (not raw SQL or Core)

The domain is small and relational. The ORM's `selectin` loader fits the response contract
cleanly. Raw SQL would mean hand-rolling row → DTO mapping for every endpoint. Core gives up
the typing without buying back perf at the scale this service realistically sees (~10⁵
books). The two places I reach for Core: bulk `pg_insert ... ON CONFLICT` for author dedupe
(`app/authors/service.py`), and the export-streaming `session.stream().partitions()` path.

---

## What I deliberately left out

Each one came up. Each one costs review surface, test surface, ops surface. Each one is
defensible to skip at this scope.

- **Authorization roles.** The spec asks for authentication, not authorization. Every
  authenticated user can mutate everything. Adding "admin can do X" without a real use case
  in the brief would be ceremony.
- **Email verification / password reset.** Real prod features. Not load-bearing for the
  design discussion.
- **Cursor pagination.** Offset is fine to ~100k rows for the access pattern shown. Switching
  to keyset-on-`(created_at, id)` is contained when it actually bites.
- **Full-text search.** Substring on `lower(title)` with an index covers the filter contract.
  Real FTS (`tsvector`, `pg_trgm`) is justified once "relevance" is a requirement.
- **Rate limiting.** The spec lists this as optional. In a real deploy, it lives in the
  gateway/sidecar layer — not in app code. Worth adding on `/auth/login` for brute-force
  protection if we keep this service public-facing.
- **Soft delete.** `book_authors.author_id ON DELETE RESTRICT` and `genre_id ON DELETE
  RESTRICT` already give the safety property `deleted_at` is usually invented for. Adding a
  tombstone invites bugs in every query that forgets the filter.
- **Caching.** Premature without a measured hot path.

---

## Testing

Tests run against **real Postgres** (`TEST_DATABASE_URL`). They are skipped automatically if
Postgres isn't reachable.

Mocked DBs were a deliberate non-choice. The most important contracts in this service —
the case-insensitive unique indexes, the `published_year` CHECK, the genre/author FK
behavior, the `updated_at` trigger, the M2M cascade — all live in Postgres. A mock that
passes while those are wrong is worse than no test.

What the tests actually verify (the spec says "we will look at what your tests verify"):

- **PATCH semantics**: unset vs. null vs. unknown key vs. empty author list (three different
  outcomes — 200 partial, 200 cleared, 422 forbid).
- **List filter combinations**: title + author + genre + year range with case-insensitive
  matching, plus the year-range cross-check (`year_from > year_to` is 422 before the DB
  sees it).
- **Author POST is strict 409 on case-insensitive duplicate** — not silently get-or-create.
- **Books with unknown author IDs return 404**, not 500 from a FK violation.
- **Auth gating**: mutating endpoints return 401; reads remain public.
- **Token reuse detection** (`tests/test_auth.py:test_refresh_reuse_revokes_all_sessions`):
  two devices, rotate device A, then present A's old token. Outcome: A's new token, B's
  token, and the stale token are *all* revoked. Verifies the load-bearing
  `session.commit()` in `auth/service.py:refresh_tokens`.
- **Logout-only-target** (`:test_logout_revokes_only_target`): explicit logout of one device
  does NOT escalate to global revoke. (This is what the `replaced_by_id IS NULL` branch
  buys us.)
- **Login timing** is roughly constant for "user not found" vs "wrong password" — both go
  through argon2.verify so trivial enumeration via timing is blocked.
- **Seeder partial failure** — exactly two rows fail and two succeed; the report identifies
  which.
- **Request id is echoed** in both `X-Request-ID` header and structured error bodies.
- **`/health` reports DB status** truthfully — fails when DB is down.

**Unit tests** (no DB, fast): `tests/test_unit_security.py` and
`tests/test_unit_validation.py` cover the pure functions: password hash roundtrip, JWT
tamper rejection, refresh token entropy, year validator bounds, CSV name splitter, row
coercion. These run in ~200ms even without Postgres.

---

## Layout

```
app/
  main.py              FastAPI app + middleware + handlers + /health
  middleware.py        Request-ID + per-request access log
  logging_config.py    JSON logging w/ request-id filter
  exceptions.py        Domain exceptions + handlers (IntegrityError → 409, Exception → 500)
  config/settings.py   pydantic-settings, env parsing, asyncpg URL fixup
  db/
    session.py         async engine/session factory + DB dependency
    models.py          ORM mapping (NOT the schema source of truth)
  auth/
    routes.py          /auth/register, /login, /refresh, /logout, /logout-all
    service.py         Token issuance + rotation + reuse detection
    security.py        Pure: argon2, JWT encode/decode, refresh-token generation
    deps.py            get_current_user_id (Bearer extraction, no DB hit)
    schemas.py
  books/
    routes.py          CRUD + /export (auth-gated mutations)
    service.py         Filter/sort/paginate, streaming export
    bulk.py            Shared import logic for the seeder
    schemas.py
  authors/
    routes.py, service.py, schemas.py
  genres/
    routes.py, schemas.py    Read-only list endpoint
  seed.py              CLI: `python -m app.seed <file>`

migrations/
  env.py
  versions/0001_initial.py   Hand-written. Schema lives here.

tests/
  conftest.py
  test_books.py              Integration: routes, auth gating, observability
  test_auth.py               Integration: login, rotation, reuse detection, logout
  test_seed.py               Integration: seeder semantics
  test_unit_security.py      Unit: argon2, JWT, refresh-token primitives
  test_unit_validation.py    Unit: year validator, name splitter, row coercion
```
