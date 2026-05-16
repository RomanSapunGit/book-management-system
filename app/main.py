from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI
from fastapi_pagination import add_pagination
from sqlalchemy import text

from app.auth.routes import router as auth_router
from app.authors.routes import router as authors_router
from app.books.routes import router as books_router
from app.config import settings
from app.db import get_engine
from app.exceptions import install_exception_handlers
from app.genres.routes import router as genres_router
from app.logging_config import configure_logging
from app.middleware import MaxBodySizeMiddleware, RequestIDMiddleware

configure_logging()
log = logging.getLogger(__name__)

app = FastAPI(
    title="Book Management API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    MaxBodySizeMiddleware,
    max_bytes=settings.bulk_import_max_bytes,
    path_prefixes=["/books/import"],
)
install_exception_handlers(app)

app.include_router(auth_router, prefix="/auth", tags=["Auth"])
app.include_router(books_router, prefix="/books", tags=["Books"])
app.include_router(authors_router, prefix="/authors", tags=["Authors"])
app.include_router(genres_router, prefix="/genres", tags=["Genres"])

# fastapi-pagination: wires the `Params` resolver and registers the OpenAPI components for
# `LimitOffsetPage[T]` response models. Must be called AFTER routers are included.
add_pagination(app)


@app.get("/health", tags=["System"])
async def health():
    """Liveness + DB connectivity. Returns 200 with `db: ok|down` so a Kubernetes-style
    probe can distinguish "process up, DB down" from "process down" without needing two endpoints.
    """
    db_status = "ok"
    try:
        async with asyncio.timeout(2):
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception as e:
        log.warning("health: db check failed: %s", e)
        db_status = "down"
    return {"status": "ok", "db": db_status}
