from __future__ import annotations

import logging
import os
from datetime import timedelta
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    def __init__(self, **values):
        super().__init__(**values)
        self._check_docker_localhost()

    def _check_docker_localhost(self):
        if os.path.exists("/.dockerenv") and "localhost" in self.database_url:
            logging.getLogger("app.config").warning(
                "DATABASE_URL points to 'localhost' while running inside Docker. "
                "This usually fails unless the database is in the same container. "
                "Check your .env file or environment variables."
            )

    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/books",
        alias="DATABASE_URL",
    )
    db_echo: bool = Field(default=False, alias="DB_ECHO")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: str = Field(default="json", alias="LOG_FORMAT")  # "json" or "plain"

    jwt_secret: str = Field(default="changeme-do-not-use-in-prod", alias="JWT_SECRET")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    access_token_ttl_minutes: int = Field(default=15, alias="ACCESS_TOKEN_TTL_MINUTES", ge=1)
    refresh_token_ttl_days: int = Field(default=30, alias="REFRESH_TOKEN_TTL_DAYS", ge=1)

    bulk_import_chunk_size: int = Field(
        default=500, alias="BULK_IMPORT_CHUNK_SIZE", ge=1, le=10_000
    )
    bulk_import_max_rows: int = Field(default=50_000, alias="BULK_IMPORT_MAX_ROWS", ge=1)
    # 2 MiB. Enforced by MaxBodySizeMiddleware. In production, also set nginx
    # `client_max_body_size` to match (primary gate; this is defense-in-depth).
    bulk_import_max_bytes: int = Field(default=2 * 1024 * 1024, alias="BULK_IMPORT_MAX_BYTES", ge=1)
    bulk_import_max_stored_errors: int = Field(
        default=1000, alias="BULK_IMPORT_MAX_STORED_ERRORS", ge=1
    )

    auth_dummy_password: str = Field(
        default="dummy-password-for-timing-equalization", alias="AUTH_DUMMY_PASSWORD"
    )

    import_rate_limit_per_window: int = Field(default=5, alias="IMPORT_RATE_LIMIT_PER_HOUR", ge=1)
    import_rate_window_seconds: int = Field(default=3600, alias="IMPORT_RATE_WINDOW_SECONDS", ge=1)

    @property
    def import_rate_window(self) -> timedelta:
        return timedelta(seconds=self.import_rate_window_seconds)

    @property
    def normalized_database_url(self) -> str:
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql+asyncpg://" + url[len("postgres://") :]
        elif url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://") :]
        for token in ("?sslmode=require", "&sslmode=require"):
            url = url.replace(token, "")
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
