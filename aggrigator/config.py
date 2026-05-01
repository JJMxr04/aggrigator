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
    # When True, dangerous endpoints — data-reset (truncate-with-CASCADE)
    # plus the manual "ingest one event by ID" — are reachable. When False
    # (default) they 403 even for authenticated admins. Flip ONLY for
    # local dev / staging — never in production.
    test_mode: bool = Field(default=False, alias="AGG_TEST_MODE")

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

    # SGO. Dev default points at the local simulator with the shared dev key
    # (``aggrigator-dev``). The simulator validates against the same value
    # — see sports-scores-sim/simulator/sportsgame/app.py. Production overrides
    # both ``SPORTSGAMEODDS_BASE_URL`` (real SGO) and ``SPORTSGAMEODDS_API_KEY``
    # (the real key) in lockstep.
    sgo_base_url: str = Field(
        default="http://127.0.0.1:8765/v2", alias="SPORTSGAMEODDS_BASE_URL"
    )
    sgo_api_key: str = Field(
        default="aggrigator-dev", alias="SPORTSGAMEODDS_API_KEY",
    )
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

    # --- free-tier tuning ---
    # Comma-separated minute values for the ingest_due_leagues cron. Default
    # "0,30" → every 30 minutes (paid SGO behavior). For free tier set to
    # "0" (hourly) to halve SGO quota burn.
    ingest_cron_minutes: str = Field(
        default="0,30", alias="AGG_INGEST_CRON_MINUTES",
    )
    # Optional weekday filter for the daily full_refresh cron. arq uses
    # 0=Mon ... 6=Sun. Empty (default) → run every day at 02:30 UTC. Set to
    # "0" to run only Mondays — usually enough to catch upstream schema
    # changes on a free tier.
    full_refresh_weekday: str = Field(
        default="", alias="AGG_FULL_REFRESH_WEEKDAY",
    )
    # Skip ingest crons (ingest_due_leagues + full_refresh) if SGO's monthly
    # /account/usage reports we're past this percent of any per-month cap
    # (requests OR entities). Set to 100 to disable the check. The check is
    # bypassed entirely when test_mode=True so tests + dev don't hit the
    # network.
    sgo_quota_threshold_pct: int = Field(
        default=90, alias="AGG_SGO_QUOTA_THRESHOLD_PCT",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def sgo_fixture_path(self) -> Path | None:
        return Path(self.sgo_fixture_dir) if self.sgo_fixture_dir else None

    @property
    def ingest_cron_minute_set(self) -> set[int]:
        """Parsed AGG_INGEST_CRON_MINUTES → set[int] for arq.cron(minute=...).

        Falls back to the 30-minute cadence on a malformed value rather than
        crashing the worker on boot.
        """
        try:
            return {
                int(p.strip())
                for p in self.ingest_cron_minutes.split(",")
                if p.strip()
            } or {0, 30}
        except ValueError:
            return {0, 30}

    @property
    def full_refresh_weekday_set(self) -> set[int] | None:
        """Parsed AGG_FULL_REFRESH_WEEKDAY → set[int] or None (= every day).

        ``None`` is meaningful here: arq's cron treats an unset weekday as
        "every day", so we return None to omit the kwarg entirely.
        """
        if not self.full_refresh_weekday.strip():
            return None
        try:
            parsed = {
                int(p.strip())
                for p in self.full_refresh_weekday.split(",")
                if p.strip()
            }
        except ValueError:
            return None
        return parsed or None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
