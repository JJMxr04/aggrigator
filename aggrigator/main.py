"""FastAPI app factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from redis.asyncio import Redis
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from aggrigator import __version__
from aggrigator.admin.views import mount_admin
from aggrigator.api import api_keys as api_keys_router
from aggrigator.api import auth as auth_router
from aggrigator.api import events as events_router
from aggrigator.api import references as references_router
from aggrigator.api import selections as selections_router
from aggrigator.api import webhook_endpoints as webhook_endpoints_router
from aggrigator.config import get_settings
from aggrigator.db import engine
from aggrigator.observability.logging import configure_logging
from aggrigator.observability.prometheus import register_metrics
from aggrigator.observability.sentry import init_sentry
from aggrigator.ops import api as ops_api_router
from aggrigator.ops import routes as ops_routes_router
from aggrigator.security.rate_limit import (
    limiter,
    rate_limit_handler,
    resolve_first_party_promotion,
)

logger = logging.getLogger(__name__)


# Defaults that ship in config.py — flagging these in prod is the cheapest
# way to catch "operator forgot to set the env var" before it bites in
# production. Kept in sync with aggrigator/config.py defaults.
_DEFAULT_JWT_SECRET = "dev-only-change-me"
_DEFAULT_FERNET_KEY = "dXuPg8VG-pEz3w6mF1gAo0FVXa5Y9xqNxGgfZ7jEz0o="
_DEFAULT_REDIS_URL = "redis://localhost:6379/0"


async def _probe_redis(redis_url: str) -> None:
    """PING the configured Redis. Log clearly on failure; don't raise — Railway
    will keep the container alive while the operator fixes the URL."""
    r = Redis.from_url(redis_url, decode_responses=True)
    try:
        ok = await r.ping()
        logger.info("[startup] redis ping ok=%s url=%s", ok, _redacted(redis_url))
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[startup] redis ping FAILED url=%s err=%s: %s — "
            "cron triggers will 503 until this is fixed. Check AGG_REDIS_URL.",
            _redacted(redis_url), type(exc).__name__, exc,
        )
    finally:
        await r.aclose()


async def _probe_db() -> None:
    """SELECT 1 against the async engine. Same don't-block-startup contract
    as the Redis probe."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("[startup] postgres ping ok")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[startup] postgres ping FAILED err=%s: %s — "
            "the app cannot serve traffic. Check AGG_DATABASE_URL "
            "(asyncpg) and AGG_DATABASE_URL_SYNC (psycopg2/alembic).",
            type(exc).__name__, exc,
        )


def _redacted(url: str) -> str:
    """Strip the password from a URL for safe logging."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    user = creds.split(":", 1)[0] if ":" in creds else creds
    return f"{scheme}://{user}:***@{host}"


def _warn_on_misconfig(settings) -> None:
    """One-shot log audit at startup: surface the most expensive-if-missed
    misconfigs (default secrets in prod, missing host allowlist, etc.)."""
    is_prod = settings.env in ("prod", "production")
    if not is_prod:
        return

    if settings.jwt_secret == _DEFAULT_JWT_SECRET:
        logger.warning(
            "[startup] AGG_JWT_SECRET is the dev default in prod — "
            "session cookies are forgeable. Generate a real value: "
            "openssl rand -hex 32"
        )
    if settings.secret_encryption_key == _DEFAULT_FERNET_KEY:
        logger.warning(
            "[startup] AGG_SECRET_ENCRYPTION_KEY is the dev default in prod — "
            "stored webhook secrets are decryptable from the source. "
            "Generate: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    if settings.redis_url == _DEFAULT_REDIS_URL:
        logger.warning(
            "[startup] AGG_REDIS_URL is the localhost default in prod — "
            "cron triggers will 503. Set ${{Redis.REDIS_URL}} from the "
            "Railway Redis plugin."
        )
    if not settings.allowed_hosts_list:
        logger.warning(
            "[startup] AGG_ALLOWED_HOSTS is empty in prod — Host-header "
            "spoofing is not blocked. Set to your Railway domain + any "
            "custom domains."
        )
    if settings.sgo_api_key in ("", "aggrigator-dev"):
        logger.warning(
            "[startup] SPORTSGAMEODDS_API_KEY is the dev placeholder in prod — "
            "every SGO call will fail. Set the real key from your SGO account."
        )
    if settings.test_mode:
        logger.warning(
            "[startup] AGG_TEST_MODE=true in prod — destructive endpoints "
            "(/ops/data-reset, /ops/ingest-event) are reachable. Disable "
            "before going public."
        )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = get_settings()
    logger.info(
        "[startup] aggrigator %s booting env=%s debug=%s log_level=%s",
        __version__, settings.env, settings.debug, settings.log_level,
    )
    _warn_on_misconfig(settings)
    await _probe_db()
    await _probe_redis(settings.redis_url)
    yield
    logger.info("[shutdown] aggrigator stopping")


def create_app() -> FastAPI:
    settings = get_settings()
    is_prod = settings.env in ("prod", "production")

    configure_logging(settings.log_level)
    init_sentry(settings)

    # In prod, kill the OpenAPI/Swagger/Redoc surfaces entirely — they
    # leak the full route + schema graph to anyone who can reach the
    # service, which is recon gold for an attacker. Dev / staging keep
    # them for developer ergonomics.
    docs_kwargs: dict = (
        {"docs_url": None, "redoc_url": None, "openapi_url": None}
        if is_prod
        else {}
    )

    app = FastAPI(
        title="Aggrigator",
        version=__version__,
        lifespan=_lifespan,
        **docs_kwargs,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)

    # Host-header allowlist (DNS-rebinding / Host-header injection defense).
    # Skipped when the env var is unset so dev / first-time deploys aren't
    # broken — operators opt in via AGG_ALLOWED_HOSTS in RAILWAY.md.
    if settings.allowed_hosts_list:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=settings.allowed_hosts_list,
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "X-Api-Key", "X-Client-App", "Content-Type"],
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.jwt_secret,
        session_cookie="aggrigator_session",
        same_site="lax",
        https_only=is_prod,
    )
    app.add_middleware(BaseHTTPMiddleware, dispatch=resolve_first_party_promotion)

    # Security headers stamped on every response. We're an internal API +
    # admin surface — never indexable, never embeddable, never leaking the
    # referrer to any third party.
    async def _security_headers_dispatch(request, call_next):
        response = await call_next(request)
        h = response.headers
        h["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "no-referrer"
        h["Permissions-Policy"] = (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
            "microphone=(), payment=(), usb=()"
        )
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        # HSTS only in prod — we're always behind Railway's HTTPS edge
        # there. In dev (HTTP localhost) HSTS would lock the browser to
        # https for the host and break local testing.
        if is_prod:
            h["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=_security_headers_dispatch)

    app.include_router(auth_router.router)
    app.include_router(api_keys_router.router)
    app.include_router(references_router.router)
    app.include_router(events_router.router)
    app.include_router(selections_router.router)
    app.include_router(webhook_endpoints_router.router)
    # Part 2.1 ops module — replaces v0 api/admin_crons.py + api/ops_console.py.
    app.include_router(ops_api_router.router)
    app.include_router(ops_routes_router.router)

    register_metrics(app)
    mount_admin(app)

    # No homepage handler. ``GET /`` falls through to FastAPI's default
    # 404 (``{"detail":"Not Found"}``), matching every other unknown
    # path. Maximum opacity — an unauthenticated probe learns nothing
    # about which routes exist or what version we're running.

    @app.get("/robots.txt", include_in_schema=False, response_class=PlainTextResponse)
    def robots():
        # The aggregator is an internal API + admin surface — block every
        # crawler from every path. Mirrors ``X-Robots-Tag: noindex`` set
        # on every response above.
        return "User-agent: *\nDisallow: /\n"

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        # Healthcheck stays minimal — Railway / docker-compose poll this.
        # Don't echo the version (one less recon datapoint for unauth'd
        # probes; logged-in operators read it from /admin or git).
        return {"ok": True}

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        return {"ok": True}

    return app


app = create_app()
