# Book Management API

Async FastAPI service for managing books and authors, backed by PostgreSQL, with JWT-based
auth (access + revocable refresh) and per-row-atomic bulk import.

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

Run the tests inside the api container (dev deps + `tests/` are baked into the image,
`TEST_DATABASE_URL` is preset):

```bash
docker compose up -d
docker exec api pytest
```

Lint:

```bash
ruff check . && ruff format --check .
```

CI runs all three (lint, tests, docker build) on every push and PR — see
`.github/workflows/ci.yml`.

---

## Environment

Loaded by `pydantic-settings` from `.env` (or process env). See `.env.example` for a
copy-pasteable starting point.

### Required in production

| Variable | Why |
|---|---|
| `JWT_SECRET` | Signs access tokens. **Must be set** to a real high-entropy value in prod — the default (`changeme-do-not-use-in-prod`) makes every token forgeable. Generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"`. |
| `DATABASE_URL` | Postgres DSN. `postgres://` and `postgresql://` schemes are auto-normalized to `postgresql+asyncpg://`. |

### Optional — with sensible defaults

| Variable | Default | Notes |
|---|---|---|
| `DB_ECHO` | `0` | SQLAlchemy statement logging. |
| `LOG_LEVEL` | `INFO` | Standard Python log level. |
| `LOG_FORMAT` | `json` | `json` for production / structured ingestion, `plain` for local readability. |
| `JWT_ALGORITHM` | `HS256` | Symmetric — `JWT_SECRET` doubles as both signing and verification key. |
| `ACCESS_TOKEN_TTL_MINUTES` | `15` | Stateless access JWT lifetime. Bounds the `/auth/logout-all` revocation gap. |
| `REFRESH_TOKEN_TTL_DAYS` | `30` | Refresh token sliding lifetime — each successful rotation grants a fresh window. |
| `BULK_IMPORT_MAX_BYTES` | `2097152` (2 MiB) | Enforced by `MaxBodySizeMiddleware`. Set nginx `client_max_body_size` to match in prod. |
| `BULK_IMPORT_CHUNK_SIZE` | `500` | Rows per inner commit batch. |
| `BULK_IMPORT_MAX_ROWS` | `50000` | Hard ceiling on rows per request, after parsing. |
| `BULK_IMPORT_MAX_STORED_ERRORS` | `1000` | Cap on `errors` array in the session record. `error_count_total` still reports the true count. |
| `IMPORT_RATE_LIMIT_PER_HOUR` | `5` | Per-user cap on `POST /books/import` attempts within the window. |
| `IMPORT_RATE_WINDOW_SECONDS` | `3600` | Window length for the import rate limit. |
| `AUTH_DUMMY_PASSWORD` | (built-in) | Hashed once at startup; verified against on `/auth/login` for unknown emails so timing matches the wrong-password case. Setting this doesn't matter functionally — it just needs to exist. |

### Test-only

| Variable | Notes |
|---|---|
| `TEST_DATABASE_URL` | When set, **overrides** `DATABASE_URL` in `tests/conftest.py`. The api container has it preconfigured to `db:5432/books_test`, which is what makes `docker exec api pytest` safe — without the override, tests would run against the prod `books` DB. |

---

## Endpoints

### Public (no auth)

| Method | Path | Notes |
|---|---|---|
| `GET` | `/books` | Filter by `title`, `author`, `author_id`, `genre`, `genre_id`, `year_from`, `year_to`. Sort + paginate. |
| `GET` | `/books/{id}` | |
| `GET` | `/books/{id}/similar` | Top-N similar books (paginated). Deterministic scoring on shared authors / same genre / year proximity. |
| `GET` | `/books/export` | Stream all matching books as JSON or CSV. |
| `GET` | `/authors` | Substring search by `q`. |
| `GET` | `/authors/{id}` | |
| `GET` | `/genres` | List of genres (seeded defaults + anything added via `POST`). |
| `GET` | `/health` | Liveness + DB connectivity (`{status, db}`). |

### Authenticated (Bearer access token)

| Method | Path | Notes |
|---|---|---|
| `POST` / `PATCH` / `DELETE` | `/books`, `/books/{id}` | Authors referenced by `author_ids` only. PATCH is strict partial (`null` clears, unknown keys → 422). |
| `POST` | `/books/import` | Multipart CSV/JSON. 2 MiB cap, 5/hour/user rate limit, per-row atomic. Returns the full import report (`ImportSessionRead`). |
| `GET` | `/books/import` | Paginated audit list of the caller's own past imports. |
| `POST` | `/authors` | Names are **not** unique — see *Author identity*. |
| `POST` / `PATCH` / `DELETE` | `/genres`, `/genres/{id}` | Case-insensitive uniqueness on name and slug. Delete is `409` if any book references the genre (FK RESTRICT). |
| `POST` | `/auth/logout-all` | Revoke every refresh token for the calling user. |

### Auth flow

| Method | Path | Notes |
|---|---|---|
| `POST` | `/auth/register` | `{email, password}` → 201 `{id, email}`. |
| `POST` | `/auth/login` | → `{access_token, refresh_token, expires_in}`. |
| `POST` | `/auth/refresh` | Rotates the refresh token. Reuse of a rotated token revokes *all* sessions. |
| `POST` | `/auth/logout` | Revoke the supplied refresh token. Idempotent. |


## Author identity — why no name uniqueness

Two real people can share a name (and occasionally a birthdate). Encoding "one person per
name" as a DB constraint was a false invariant — see migration `0004_drop_authors_name_unique`.

Consequences:

- `POST /authors` accepts any name, including exact duplicates. Two `"John Smith"` rows
  with distinct ids are valid.
- Bulk import always **creates new author rows** per input name (it has no way to know
  which "John Smith" the CSV means). Clients that want dedup should pass `author_ids`
  through the API instead of names through import.

---

## Bulk import — `POST /books/import`

Multipart CSV or JSON upload. Auth required. Hard 2 MiB cap and 5/hour/user rate limit.

**Per-row atomicity.** Each row runs inside `session.begin_nested()` (a SAVEPOINT). A row
that fails — validation, unknown genre, integrity violation — rolls back to its savepoint;
the rest of the batch still commits. The response carries the full report:

```json
{
  "id": "...", "status": "completed",
  "received": 4, "successful": 2, "failed": 2,
  "errors": [{"row": 2, "reason": "title must not be empty"}, ...],
  "error_count_total": 2
}
```

**The session row is written before processing.** This means:

- A top-level parse failure (bad JSON, missing CSV header) still records an
  `import_sessions` row and counts toward the rate limit.
- `GET /books/import` returns the caller's full history, even for attempts that 400'd at
  the parse step.

**CSV/JSON shape** (names, not ids — the importer resolves authors and genres by name):

```csv
title,genre,authors,published_year,description
The Hobbit,Fantasy,J. R. R. Tolkien;Christopher Tolkien,1937,
```

Authors use `;` or `|` as a separator (commas conflict with CSV). `genre` is the genre's
name or slug (case-insensitive). Example payloads live in `examples/`.
---

## Auth — token lifecycle

Two-token flow:

- **Access token**: signed JWT, **15 min**, *stateless* (no DB hit per request).
- **Refresh token**: 256 random bits, **30 days**, **stored as `sha256(token)` in
  `refresh_tokens`**. Rotated on every successful `/auth/refresh`; the old row is marked
  `revoked_at` with `replaced_by_id` pointing at its successor.

### Why a DB-backed refresh

If we want real logout / device revocation, *something* has to be stateful. A
`refresh_tokens` row is the cleanest option — touched only on `/auth/refresh` (~every 15
min per active session), not on every API request. The cost is one indexed lookup per
token-renewal.

### Why argon2 for passwords but sha256 for refresh tokens

Passwords are low-entropy (`hunter2`-class). Slow hashing protects them by making offline
brute-force expensive. Refresh tokens are 256 random bits — there is no "brute force" to
slow down; we just need confidentiality at rest. Argon2 here would be a category error.

### Reuse detection

Refresh tokens rotate on every use. If a token that has *already been rotated* is
presented again, that's a signal someone has a copy they shouldn't — almost always token
theft. The service:

1. Logs a warning with the user id.
2. Revokes every active refresh token for that user.
3. **Commits the revocation before raising** — otherwise the session dependency rolls back
   and the attacker keeps their access. (See `app/auth/service.py:refresh_tokens` — the
   explicit commit is load-bearing, not boilerplate.)
4. Returns 401.

This is distinct from "revoked via logout" (`replaced_by_id IS NULL`). Logging out a token
twice is idempotent and never escalates — only *rotated-then-reused* triggers the global
revoke.

### Caveat — logout-all and the 15-min gap

Access tokens are stateless. `POST /auth/logout-all` revokes all refresh tokens
immediately, but any access token already issued is good until its 15-min TTL expires. The
check (jti denylist or `token_version` on every request), which is the standard JWT
trade-off and intentionally not built here.

---

## Observability — "which request caused that 500?"

Every request gets a UUID (or an inbound `X-Request-ID` from a gateway). It appears in:

- The response header `X-Request-ID`.
- The structured JSON log lines emitted during the request (`request_id_var` ContextVar
  → JSON formatter — `app/middleware.py`, `app/logging_config.py`).
- Every error body. The 500 handler deliberately does NOT leak the exception message
  — only the `request_id`. The full traceback goes to logs.

Workflow when a user reports a 500:

```bash
docker compose logs api | grep <request_id>
```

You get the inbound access-log line, any intermediate warnings, and the `unhandled
exception` line with the traceback — all keyed by the same id.

`/health` checks DB connectivity (2s timeout) and returns `{status, db}` — a probe can
distinguish "process up, DB down" from "process down."

---

## Recommendation — `GET /books/{id}/similar`

Hand-rolled scoring, computed inside one SQL query. No ML.

- `+3` shared author
- `+2` same genre
- `+1` published within ±5 years

Source book excluded; results filtered to `score > 0`. Ordered by `score DESC,
created_at DESC, id`.

**Pre-filtered candidate set.** Before scoring, the query restricts the scanned set to
books that share at least one signal with the source (any-shared-author OR same-genre OR
nearby-year). Without the pre-filter, the DB would compute `CASE` arithmetic against every
row in `books` — fine at 1k rows, painful at 1M. With the pre-filter, scoring runs over
what the indexes can find.

Migrations are **hand-written**, never autogenerated. `app/db/models.py` is the ORM
mapping; the migrations are the source of truth for schema (functional indexes, CHECKs, FK
behavior, seed genres, `updated_at` triggers). `tests/conftest.py` mirrors the
migration-only artifacts manually so tests match production.

### Notable schema choices

- **`books.genre_id ON DELETE RESTRICT`** — refusing to delete a referenced genre is the
  safe default. CASCADE would silently destroy data; tombstones invite query-filter bugs.
- **`book_authors.author_id ON DELETE RESTRICT`** — same shape for authors.
- **`refresh_tokens.user_id ON DELETE CASCADE`** — when a user is deleted, their tokens
  go too.
- **`CHECK (published_year >= 1800)`** — the floor is in the DB; the upper bound is
  Pydantic-only because CHECK must be immutable.
- **`updated_at` trigger** — keeps the column truthful even for raw SQL updates that
  bypass the ORM.

### Why M2M (`book_authors`) and not `books.author_id`

Books with multiple authors are common. A single FK forces a lie ("primary author"). The
M2M cost is one join table; the simplification cost of `1:N` is permanent data fidelity
loss.


## Tests

Four files, 15 tests, ~3 seconds against a real Postgres.

- `tests/test_e2e.py` — two full user journeys (curator lifecycle + bulk import flow).
  Each starts with an unauthenticated 401 probe to pin auth gating.
- `tests/test_auth.py` — the four refresh/logout security branches.
- `tests/test_invariants.py` — cross-cutting properties: year bounds, PATCH empty-authors
  → 409, FK RESTRICT on genre delete, similar-books scoring weights, import per-row
  atomicity, 413 size cap, 429 rate limit.
- `tests/test_unit.py` — pure-Python checks: the JWT `type="access"` contract and
  `_split_names` separator handling.

Tests run against real Postgres (not mocks) because the load-bearing invariants — FK
RESTRICT, CHECKs, case-insensitive functional indexes, the `updated_at` trigger — all live
in the DB. A mock that passes while those are wrong is worse than no test.

---

## Layout

```
app/
  main.py              FastAPI app + middleware + handlers + /health
  middleware.py        Request-ID + body-size limiter
  logging_config.py    JSON logging w/ request-id filter
  exceptions.py        Domain exceptions + handlers (IntegrityError → 409, Exception → 500)
  config/settings.py   pydantic-settings, env parsing, asyncpg URL fixup
  db/
    session.py         async engine/session factory + DB dependency
    models.py          ORM mapping (NOT the schema source of truth)
  auth/
    routes.py, service.py, security.py, deps.py, schemas.py
  books/
    routes.py          CRUD + /export + /import + /similar
    service.py         Filter/sort/paginate; similar-books scoring; streaming export
    bulk.py            CSV/JSON parse + per-row import w/ savepoints
    import_deps.py     Per-user-per-hour rate limit
    schemas.py
  authors/, genres/    routes.py, service.py, schemas.py

migrations/versions/
  0001_initial.py                       Hand-written. Schema lives here.
  0002_import_sessions.py
  0003_genres_case_insensitive.py
  0004_drop_authors_name_unique.py

tests/
  conftest.py
  test_e2e.py          Full user journeys
  test_auth.py         Refresh rotation + reuse detection + logout
  test_invariants.py   Cross-cutting properties
  test_unit.py         Pure-Python: JWT type contract, name splitter

.github/workflows/ci.yml   Parallel lint + tests + docker build on push/PR.
```
