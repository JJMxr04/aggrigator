"""Pydantic-settings — env-driven config for the aggregator.

All env vars are prefixed ``AGG_`` except ``ODDSAPI_API_KEY`` which keeps
the upstream convention.
See ``.env.example`` for the full list.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Hardcoded path on the MDProject receiver — see
# ``MDProject/CoreRoot/urls.py`` (``path('sportgameodds/webhook', ...)``).
# Operator sets ``AGG_MDPROJECT_URL`` to the base URL and we append this.
MDPROJECT_WEBHOOK_PATH = "/sportgameodds/webhook"


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

    # --- odds-api.io ---
    # Base URL is hardcoded in aggrigator/ingest/odds_api_http.py as the
    # OddsApiHttpClient kwarg default — there is no env override.
    odds_api_key: str = Field(default="", alias="ODDSAPI_API_KEY")
    # Per-hour throttle: skip auto crons when current usage is ≥ this %% of
    # the live ``x-ratelimit-limit``. 80 means "skip when 80/100 req used on
    # free, 4000/5000 on paid Starter."
    odds_api_throttle_pct: int = Field(
        default=80, alias="AGG_ODDSAPI_THROTTLE_PCT",
    )
    # NOTE: bookmaker list is HARDCODED at 2 in code (see
    # aggrigator/ingest/odds_api_http.py:ODDSAPI_BOOKMAKERS) by design —
    # rationale in aggrigator-plan/odds-api/best-practices.md §4.1. Even
    # on paid tiers we stay at 2 books. There is intentionally no env var
    # for this.
    # On HTTP 429, retry up to this many times with exponential backoff
    # (honors Retry-After header when present). 0 = no retries (fail fast).
    odds_api_max_retries: int = Field(
        default=3, alias="AGG_ODDSAPI_MAX_RETRIES",
    )
    # (TheSportsDB was retired in Phase 16 — odds-api is the sole source. The
    # THESPORTSDB_API_KEY setting was removed along with the historical ingest.)

    # auth
    jwt_secret: str = Field(default="dev-only-change-me", alias="AGG_JWT_SECRET")
    # Independent secret for Starlette's SessionMiddleware (signs the
    # admin/ops session cookie). Empty → dev-only fallback to jwt_secret
    # via ``resolved_session_secret``. In prod an empty value (or one equal
    # to AGG_JWT_SECRET) is a fatal boot error — see _enforce_prod_secrets
    # in main.py — so JWT forgery and admin-session forgery never share a
    # single leaked key.
    session_secret: str = Field(default="", alias="AGG_SESSION_SECRET")

    @property
    def resolved_session_secret(self) -> str:
        """Session-cookie signing key: AGG_SESSION_SECRET when set, else a
        dev-only fallback to jwt_secret. Prod can't reach the fallback —
        boot enforcement requires a distinct dedicated value. Single source
        for BOTH the app SessionMiddleware and the SQLAdmin auth backend,
        so the two can never drift onto different keys again."""
        return self.session_secret or self.jwt_secret
    jwt_access_ttl_seconds: int = Field(default=900, alias="AGG_JWT_ACCESS_TTL_SECONDS")
    jwt_refresh_ttl_seconds: int = Field(
        default=14 * 24 * 3600, alias="AGG_JWT_REFRESH_TTL_SECONDS"
    )

    # Base URL of the MDProject receiver, e.g.
    # ``https://mdproject-production.up.railway.app``. The actual webhook
    # path (``MDPROJECT_WEBHOOK_PATH``) is hardcoded — operator only configures
    # the host. Empty default so dev boots without one; the dispatcher logs
    # and skips delivery if unset.
    mdproject_url: str = Field(default="", alias="AGG_MDPROJECT_URL")
    # HMAC-SHA256 secret shared with MDProject's AGGRIGATOR_WEBHOOK_SECRET.
    # Used by aggrigator/security/webhook_signing.sign() at delivery time.
    webhook_secret: str = Field(default="", alias="AGG_WEBHOOK_SECRET")

    @property
    def webhook_target_url(self) -> str:
        """Full URL to POST webhooks to. Derived from ``mdproject_url`` +
        the hardcoded receiver path. Empty when ``mdproject_url`` is unset."""
        if not self.mdproject_url:
            return ""
        return f"{self.mdproject_url.rstrip('/')}{MDPROJECT_WEBHOOK_PATH}"

    # Identifies THE service tenant (plan §6.4: one key for all MDProject
    # traffic). When a request's key belongs to the tenant user whose
    # external_user_id equals this UUID, it may assert X-Acting-User to
    # act for any tenant user on per-user-data routes (bets). Empty
    # (default) = no tenant may assert — per-user keys keep working
    # unchanged. Set in Coolify when the service tenant is provisioned
    # (Phase 2 of the roadmap).
    service_tenant_external_id: str = Field(
        default="", alias="AGG_SERVICE_TENANT_EXTERNAL_ID",
    )

    # P0-5 transitional flag (plan §6.2): when False (default), keyless
    # requests to the read surface (references/events/selections) are
    # allowed but WARN-logged — visibility into what would break. Flip to
    # True in prod ONLY after MDProject attaches the service key and the
    # warnings hit zero; later the flag dies and keyed reads become
    # unconditional. A key that IS present gets validated either way —
    # fail closed during the transition, or flipping the flag would break
    # a silently-misconfigured client.
    require_key_for_reads: bool = Field(
        default=False, alias="AGG_REQUIRE_KEY_FOR_READS",
    )

    # Bearer token for /metrics (plan §6.2 P0-4). Set in Coolify + the
    # Prometheus scraper config; mismatch returns 404 (hide-existence,
    # same pattern as the profiler). Empty = open endpoint + prod boot
    # warning.
    metrics_token: str = Field(default="", alias="AGG_METRICS_TOKEN")

    # The aggrigator's SECURE_PROXY_SSL_HEADER equivalent (MDProject's
    # Django fix for the same problem, CoreRoot/settings.py). Behind the
    # Cloudflare-tunnel topology, TLS terminates at Cloudflare and Traefik
    # receives plain http — it then OVERWRITES X-Forwarded-Proto to "http"
    # because cloudflared isn't a trusted proxy IP. The app then builds
    # http:// absolute URLs (sqladmin url_for redirects), which Chrome
    # blocks under CSP form-action when a form submission gets redirected
    # to them. True = force scope["scheme"]="https" on every request:
    # correct whenever the ONLY public ingress is https (it is — Cloudflare
    # redirects all http at the edge).
    force_https: bool = Field(default=False, alias="AGG_FORCE_HTTPS")

    # --- rate limiting (security/rate_limit.py) ---
    # Master switch — leave True everywhere; False exists for debugging only.
    ratelimit_enabled: bool = Field(default=True, alias="AGG_RATELIMIT_ENABLED")
    # limits-style storage URL. Empty → in-process memory (dev/tests).
    # Prod MUST point at the shared Redis resource (redis://...) — with 4
    # uvicorn workers, memory counters allow 4× every limit. Loud boot
    # warning when memory is used in prod.
    ratelimit_storage_url: str = Field(
        default="", alias="AGG_RATELIMIT_STORAGE_URL",
    )

    # Inbound "Paradise" channel: HMAC secret for /v1/internal/* signed by
    # MDProject. Same value as MDProject's PARADISE_SECRET. NEVER reuse
    # webhook_secret — opposite directions, separate trust scopes. Empty
    # default → /v1/internal/* deps reject every request with 503 until
    # the operator sets the env var.
    paradise_secret: str = Field(default="", alias="AGG_PARADISE_SECRET")

    # CORS
    cors_origins: str = Field(
        default="http://localhost:3000,http://localhost:8000",
        alias="AGG_CORS_ORIGINS",
    )

    # Host-header allowlist. Comma-separated. Empty (default) skips the
    # TrustedHostMiddleware install — current behavior. Set in prod to
    # e.g. "agg.example.com,api.example.com" to reject Host-spoofed
    # requests. Wildcard prefixes ("*.example.com") are honored by
    # Starlette.
    #
    # Coolify auto-merge: when this is non-empty, ``allowed_hosts_list``
    # also appends any hostnames Coolify injects via ``COOLIFY_FQDN`` so
    # operators don't have to copy the platform-generated subdomain in
    # by hand. Set this to your custom domain (or even a placeholder
    # like the Coolify FQDN itself) to enable validation.
    allowed_hosts: str = Field(default="", alias="AGG_ALLOWED_HOSTS")

    # FastAPI's auto-generated /docs, /redoc, /openapi.json. OFF BY
    # DEFAULT — they leak the full route + schema graph, which is recon
    # material for any unauthenticated probe. Flip to True only when
    # actively developing/testing the API surface, and only on
    # short-lived environments.
    docs_enabled: bool = Field(default=False, alias="AGG_DOCS_ENABLED")

    # --- profilers (pyinstrument + sqltap) ---
    # OFF by default; flip ON for a debugging session and back OFF when
    # done. Both add real overhead — sqltap accumulates query history in
    # worker memory, pyinstrument samples the call stack at 1ms intervals
    # on every profiled request. Safe in staging; avoid leaving on in
    # prod for long. See aggrigator/observability/profiler.py.
    profiler_enabled: bool = Field(
        default=False, alias="AGG_PROFILER_ENABLED",
    )
    # Shared secret. Required as the X-Profile-Token header (or
    # ``?token=`` query param) on every profiler request. When unset,
    # the endpoints 404 even if profiler_enabled=True — a paranoid
    # default so a forgotten flag flip can't expose query text and
    # stack traces to the public internet.
    profiler_token: str = Field(
        default="", alias="AGG_PROFILER_TOKEN",
    )

    # --- error tracking (GlitchTip via sentry-sdk) ---
    # DSN from the GlitchTip ``aggrigator`` project. Empty (default) =
    # sentry stays inert — local dev needs nothing. Web and worker share
    # the same DSN; ``environment`` comes from ``env`` above and
    # ``release`` from the Coolify-injected SOURCE_COMMIT build arg.
    # See aggrigator/observability/sentry.py.
    sentry_dsn: str = Field(default="", alias="AGG_SENTRY_DSN")

    # --- ingest window (storage + quota tuning) ---
    # Cron times are hardcoded next to each task in workers/tasks/ via
    # ``@app.periodic(cron=...)``. To change cadence, edit that decorator
    # and redeploy (cadence is product policy, not infra knob).
    #
    # ``ingest_due_leagues`` only walks events whose ``start_time`` falls
    # in [now - days_behind, now + days_ahead]. Cuts both directions of cost:
    #   - Fewer provider entities returned per /events call (per-month quota).
    #   - Fewer Event/Market/Selection rows inserted (DB storage).
    # The "behind" buffer catches events that are LIVE right now (they
    # started in the past) plus just-finished games still flowing through
    # the lifecycle → settle pipeline. Combined with skip_if_new_terminal,
    # nothing older than this window leaks into the DB.
    # 30-day default matches the MDProject portal's "upcoming" horizon
    # (it asks for events up to 90 days out, but 30 is the common-case
    # planning window). Per-month odds-api entity quota scales linearly
    # with the ahead-window — bumping from 7 → 30 is ~4× quota for events
    # in that range. If you start hitting the per-month cap, narrow this
    # back down via the env override before lowering ingest cadence.
    ingest_window_days_ahead: int = Field(
        default=30, alias="AGG_INGEST_WINDOW_DAYS_AHEAD",
    )
    ingest_window_days_behind: int = Field(
        default=2, alias="AGG_INGEST_WINDOW_DAYS_BEHIND",
    )

    # --- ingest response-shape tuning (bandwidth + DB rows) ---
    # ``include_alt_lines``: include every alt spread/total by default
    # (e.g. for an MLB total of 8.5, also 7.5/8/9/9.5/...). Default
    # False — we don't render or grade them and they bloat the
    # bookmaker_selection table with rows we never read. Currently
    # ignored by the odds-api client; honored if a future client
    # supports it.
    ingest_include_alt_lines: bool = Field(
        default=False, alias="AGG_INGEST_INCLUDE_ALT_LINES",
    )
    # Comma-separated oddID list to restrict markets to (e.g.
    # "points-game-ml-home,points-game-ml-away" for moneyline only).
    # Empty → no filter. Currently ignored by the odds-api client.
    ingest_odd_ids: str = Field(
        default="", alias="AGG_INGEST_ODD_IDS",
    )
    # Bookmaker ID(s) to restrict to, comma-separated. Empty → all.
    # NOTE: odds-api uses the hardcoded ODDSAPI_BOOKMAKERS list in
    # ingest/odds_api_http.py — this setting is currently a no-op.
    ingest_bookmaker_id: str = Field(
        default="", alias="AGG_INGEST_BOOKMAKER_ID",
    )
    # When True (default), the cron drops events arriving in a terminal
    # status (finished/postponed/canceled) if our DB has never seen them
    # — avoids backfilling ancient games every walk in steady state.
    # Flip to False for one run after wiping the DB or seeding a fresh
    # deployment so today's already-finished games actually get inserted.
    # Then either flip back to True (matches old behavior) or leave it
    # False (the time window already prevents ancient backfill).
    ingest_skip_new_terminal: bool = Field(
        default=True, alias="AGG_INGEST_SKIP_NEW_TERMINAL",
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

    # --- lifecycle watchdog (stale-event detection + auto-void) ---
    # Events whose start_time is in the past but the provider still reports as
    # ``notstarted`` / ``inprogress`` are "stale" — most often a postponed
    # game the provider never updated. The watchdog runs hourly and surfaces them
    # via the computed ``stale`` flag on the API + ops dashboard so an
    # operator can investigate. After ``stale_grace_hours`` past start_time,
    # an event is reported stale (informational only — no DB writes).
    lifecycle_stale_grace_hours: int = Field(
        default=12, alias="AGG_LIFECYCLE_STALE_GRACE_HOURS",
    )
    # If > 0, events still ``notstarted`` / ``inprogress`` more than this
    # many hours past start_time are presumptively VOIDED: status_type
    # flipped to ``canceled``, ``feed_locked=True``, all PENDING selections
    # voided, ``event.voided`` webhook fires. 0 (default) DISABLES auto-void
    # — the watchdog only flags stale events without touching them. Set to
    # something like 48 once you trust it. Recovery: if the provider later returns
    # real scores, the next ingest sees ``canceled → finished`` and
    # PROVIDER grading overrides the COMPUTED-source VOID rows.
    lifecycle_auto_void_hours: int = Field(
        default=0, alias="AGG_LIFECYCLE_AUTO_VOID_HOURS",
    )

    # --- disappearance detection (odds-api.io) ---
    # Independent of the stale-pending clock above: a pre-match cancellation
    # 6h before kickoff has to be catchable even though stale-pending only
    # fires after start time. See cancelled-suspended.md §3 Layer 2.
    #
    # ``..._grace_hours``: an event that hasn't been seen upstream for
    # this many hours is reported as disappeared (informational).
    # ``..._void_hours``: past this threshold, the watchdog auto-voids it
    # (same VOID path as the stale-pending branch). 0 disables auto-void
    # for the disappearance signal.
    lifecycle_disappeared_grace_hours: int = Field(
        default=6, alias="AGG_LIFECYCLE_DISAPPEARED_GRACE_HOURS",
    )
    lifecycle_disappeared_void_hours: int = Field(
        default=12, alias="AGG_LIFECYCLE_DISAPPEARED_VOID_HOURS",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_hosts_list(self) -> list[str]:
        explicit = [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]
        # Auto-merge Coolify's injected FQDN(s) so operators don't have to
        # copy the platform-generated hostname into AGG_ALLOWED_HOSTS. Only
        # appended when the operator has already opted in to validation by
        # setting AGG_ALLOWED_HOSTS — empty stays empty (middleware off in
        # main.py) so first-boot deploys aren't surprise-locked.
        if not explicit:
            return []
        coolify_fqdn = os.environ.get("COOLIFY_FQDN", "")
        coolify = [h.strip() for h in coolify_fqdn.split(",") if h.strip()]
        # Loopback is always allowed once validation is on: the Docker
        # HEALTHCHECK curls http://127.0.0.1:$PORT/healthz from inside
        # the container, and TrustedHostMiddleware would otherwise 400 it
        # → Coolify marks the deploy unhealthy and rolls back (bitten
        # 2026-06-12). No spoofing value: an attacker-supplied Host of
        # 127.0.0.1 can't poison links toward an attacker domain.
        loopback = ["127.0.0.1", "localhost"]
        seen: set[str] = set()
        out: list[str] = []
        for h in (*explicit, *coolify, *loopback):
            if h not in seen:
                seen.add(h)
                out.append(h)
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
