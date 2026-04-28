"""Pydantic-settings — env-driven config for the aggregator.

All env vars are prefixed ``AGG_`` except those that already have an upstream
convention (``SPORTSGAMEODDS_*``). See ``.env.example`` for the full list.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # core
    env: str = Field(default="dev", alias="AGG_ENV")
    debug: bool = Field(default=False, alias="AGG_DEBUG")
    log_level: str = Field(default="INFO", alias="AGG_LOG_LEVEL")

    # db — local dev defaults match Homebrew Postgres 18 on port 5434.
    database_url: str = Field(
        default="postgresql+asyncpg://aggrigator:aggrigator@localhost:5434/aggrigator",
        alias="AGG_DATABASE_URL",
    )
    database_url_sync: str = Field(
        default="postgresql+psycopg2://aggrigator:aggrigator@localhost:5434/aggrigator",
        alias="AGG_DATABASE_URL_SYNC",
    )

    # redis — Homebrew default. docker-compose uses 6380 to coexist.
    redis_url: str = Field(default="redis://localhost:6379/0", alias="AGG_REDIS_URL")

    # SGO
    sgo_base_url: str = Field(
        default="http://127.0.0.1:8765/v2", alias="SPORTSGAMEODDS_BASE_URL"
    )
    sgo_api_key: str = Field(default="", alias="SPORTSGAMEODDS_API_KEY")
    sgo_fixture_dir: str = Field(default="", alias="SPORTSGAMEODDS_FIXTURE_DIR")

    # auth
    jwt_secret: str = Field(default="dev-only-change-me", alias="AGG_JWT_SECRET")
    jwt_access_ttl_seconds: int = Field(default=900, alias="AGG_JWT_ACCESS_TTL_SECONDS")
    jwt_refresh_ttl_seconds: int = Field(
        default=14 * 24 * 3600, alias="AGG_JWT_REFRESH_TTL_SECONDS"
    )

    # Symmetric encryption key for webhook signing secrets at rest.
    # Generate prod values with: ``Fernet.generate_key().decode()``.
    secret_encryption_key: str = Field(
        default="dXuPg8VG-pEz3w6mF1gAo0FVXa5Y9xqNxGgfZ7jEz0o=",
        alias="AGG_SECRET_ENCRYPTION_KEY",
    )

    # CORS
    cors_origins: str = Field(
        default="http://localhost:3000,http://localhost:8000",
        alias="AGG_CORS_ORIGINS",
    )

    # observability
    sentry_dsn: str = Field(default="", alias="AGG_SENTRY_DSN")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def sgo_fixture_path(self) -> Path | None:
        return Path(self.sgo_fixture_dir) if self.sgo_fixture_dir else None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
