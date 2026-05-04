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

    # Pre-emptive throttle: minimum seconds between SGO HTTP requests on a
    # single client instance. Default 0 — burst 5–10 calls in <1s and let
    # the 429 retry-with-backoff handle the rare overshoot. Tested live
    # against SGO's free tier (10/min cap) — bursts well under the cap
    # never trip 429. Set to a positive value (e.g. 7.0) only if you're
    # seeing chronic 429s on your tier.
    sgo_min_interval_seconds: float = Field(
        default=0.0, alias="SPORTSGAMEODDS_MIN_INTERVAL_SECONDS",
    )
    # On HTTP 429, retry up to this many times with exponential backoff
    # (honors Retry-After header when present). 0 = no retries (fail fast).
    sgo_max_retries: int = Field(
        default=3, alias="SPORTSGAMEODDS_MAX_RETRIES",
    )

    # auth
    jwt_secret: str = Field(default="dev-only-change-me", alias="AGG_JWT_SECRET")
    # Independent secret for Starlette's SessionMiddleware (signs the
    # admin/ops session cookie). Empty → falls back to jwt_secret with a
    # WARNING. Set in prod so JWT and session can be rotated independently
    # and a leak of one doesn't compromise the other.
    session_secret: str = Field(default="", alias="AGG_SESSION_SECRET")
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

    # Host-header allowlist. Comma-separated. Empty (default) skips the
    # TrustedHostMiddleware install — current behavior. Set in prod to
    # e.g. "aggrigator-production.up.railway.app,api.example.com" to
    # reject Host-spoofed requests. Wildcard prefixes ("*.example.com")
    # are honored by Starlette.
    allowed_hosts: str = Field(default="", alias="AGG_ALLOWED_HOSTS")

    # observability
    sentry_dsn: str = Field(default="", alias="AGG_SENTRY_DSN")

    # FastAPI's auto-generated /docs, /redoc, /openapi.json. OFF BY
    # DEFAULT — they leak the full route + schema graph, which is recon
    # material for any unauthenticated probe. Flip to True only when
    # actively developing/testing the API surface, and only on
    # short-lived environments.
    docs_enabled: bool = Field(default=False, alias="AGG_DOCS_ENABLED")

    # --- free-tier tuning ---
    # Comma-separated minute values for the ``ingest_event_lifecycle`` cron
    # (formerly drove ``ingest_due_leagues`` — that combined cron is now
    # manual-only / used by ``full_refresh``). Default "0,30" = every 30
    # min. The lifecycle phase is the cheap half (event rows + status +
    # settlement + webhooks) so this can run frequently without the
    # bookmaker-quote write cost.
    ingest_cron_minutes: str = Field(
        default="0,30", alias="AGG_INGEST_CRON_MINUTES",
    )
    # Comma-separated hour values for the ``ingest_event_odds`` cron. Default
    # "0,2,4,...,22" = every 2 hours. The odds phase is the expensive half
    # (per-bookmaker price writes) — run it less often so it doesn't dominate
    # the worker. ALWAYS schedule odds AFTER lifecycle has had a chance to
    # create event rows for that window — see odds_cron_minute below.
    odds_cron_hours: str = Field(
        default="0,2,4,6,8,10,12,14,16,18,20,22",
        alias="AGG_ODDS_CRON_HOURS",
    )
    # Minute-of-the-hour for ``ingest_event_odds``. Defaults to 15 so it
    # lands ~15 min AFTER a lifecycle run at :00 — by that point any new
    # event rows lifecycle created are visible to the odds walk.
    odds_cron_minute: int = Field(
        default=15, alias="AGG_ODDS_CRON_MINUTE",
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
    # Day-of-month (1-28; clamped) when SGO's per-month entity counter
    # resets. Free/amateur tier resets at calendar month start (=1) but
    # paid tiers reset on the customer's signup-anniversary day.
    # The proportional pacer (``quota_status``) uses this to compute
    # how much of the cycle has elapsed and what % of cap is "fair to
    # have used by now" — anything beyond projects to overshoot the
    # threshold and skips the auto run.
    sgo_quota_reset_day: int = Field(
        default=1, alias="AGG_SGO_QUOTA_RESET_DAY",
    )
    # Warm-up grace: regardless of pacing, allow current usage up to
    # this % of cap before the proportional check kicks in. Without
    # this, every run at hour 1 of the cycle would block (any usage
    # extrapolates to a huge end-of-cycle projection). Default 5%
    # leaves room for one big seed/full_refresh at cycle start.
    sgo_quota_pace_floor_pct: int = Field(
        default=5, alias="AGG_SGO_QUOTA_PACE_FLOOR_PCT",
    )

    # --- ingest window (storage + quota tuning) ---
    # ``ingest_due_leagues`` only walks SGO events whose ``start_time`` falls
    # in [now - days_behind, now + days_ahead]. Cuts both directions of cost:
    #   - Fewer SGO entities returned per /events call (per-month quota).
    #   - Fewer Event/Market/Selection rows inserted (DB storage).
    # The 1-day "behind" buffer catches events that are LIVE right now (they
    # started in the past) plus just-finished games still flowing through
    # the lifecycle → settle pipeline. Combined with skip_if_new_terminal,
    # nothing older than this window leaks into the DB.
    ingest_window_days_ahead: int = Field(
        default=7, alias="AGG_INGEST_WINDOW_DAYS_AHEAD",
    )
    ingest_window_days_behind: int = Field(
        default=1, alias="AGG_INGEST_WINDOW_DAYS_BEHIND",
    )

    # --- ingest entity-cost tuning (per-month quota) ---
    # Each ``/events`` call returns events × markets × selections ×
    # bookmakers, all counted as entities against the per-month cap.
    # These knobs trim the response server-side so you spend fewer
    # entities per call without changing the call count.
    #
    # ``include_alt_lines``: SGO returns every alt spread/total by
    # default (e.g. for an MLB total of 8.5, also 7.5/8/9/9.5/...).
    # That's typically 5–10× the entities of main lines alone. Default
    # False here — flip on per-cron only if you have a use case for
    # alt lines (we don't currently render or grade them).
    ingest_include_alt_lines: bool = Field(
        default=False, alias="AGG_INGEST_INCLUDE_ALT_LINES",
    )
    # ``odd_ids``: comma-separated list of SGO oddID strings to
    # restrict to (e.g. "points-game-ml-home,points-game-ml-away"
    # for moneyline only). Empty → no filter, all markets returned.
    # Use to skip player props / alt markets when you only need
    # ML/spread/total.
    ingest_odd_ids: str = Field(
        default="", alias="AGG_INGEST_ODD_IDS",
    )
    # ``bookmaker_id``: SGO bookmaker ID(s) to restrict to. Comma-
    # separated for multiple (SGO accepts e.g.
    # ``bookmakerID=draftkings,fanduel,betmgm``). Empty → all bookmakers
    # (the aggregator default). Restricting cuts per-event entity cost
    # roughly linearly with the number of books selected. Don't narrow
    # so far you lose the multi-bookmaker value prop.
    ingest_bookmaker_id: str = Field(
        default="", alias="AGG_INGEST_BOOKMAKER_ID",
    )

    # --- vacuum (storage tuning for free DB tiers) ---
    # Delete terminal events older than this many days. The vacuum cron
    # runs nightly @ 04:00 UTC. Set to 0 to disable (the cron stays
    # registered but each run is a no-op). Default 3 — safe for free
    # Neon (0.5 GB) while leaving a window for late settlements + webhook
    # redeliveries.
    vacuum_days: int = Field(default=3, alias="AGG_VACUUM_DAYS")
    # Max events deleted per cron run. Cascades through markets, selections,
    # quotes, and webhook_delivery via DB FKs, so the actual row count
    # touched is much higher. Keep modest so a backlog doesn't lock the
    # DB or blow Neon's connection budget.
    vacuum_batch_size: int = Field(default=1000, alias="AGG_VACUUM_BATCH_SIZE")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

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
    def odds_cron_hour_set(self) -> set[int]:
        """Parsed AGG_ODDS_CRON_HOURS → set[int] for arq.cron(hour=...)."""
        try:
            parsed = {
                int(p.strip())
                for p in self.odds_cron_hours.split(",")
                if p.strip()
            }
        except ValueError:
            parsed = set()
        # Sane fallback: every 2 hours.
        return parsed or {0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22}

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
