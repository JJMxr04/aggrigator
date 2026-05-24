"""FastAPI app factory."""

from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from aggrigator import __version__
from aggrigator.admin.views import mount_admin
from aggrigator.api import analytics as analytics_router
from aggrigator.api import auth as auth_router
from aggrigator.api import bets as bets_router
from aggrigator.api import events as events_router
from aggrigator.api import internal as internal_router
from aggrigator.api import references as references_router
from aggrigator.api import selections as selections_router
from aggrigator.config import get_settings
from aggrigator.db import engine
from aggrigator.observability.logging import configure_logging
from aggrigator.observability.prometheus import register_metrics
from aggrigator.ops import api as ops_api_router
from aggrigator.ops import routes as ops_routes_router
from aggrigator.workers.app import app as procrastinate_app

logger = logging.getLogger(__name__)


# Defaults that ship in config.py — flagging these in prod is the cheapest
# way to catch "operator forgot to set the env var" before it bites in
# production. Kept in sync with aggrigator/config.py defaults.
_DEFAULT_JWT_SECRET = "dev-only-change-me"


async def _probe_db() -> None:
    """SELECT 1 against the async engine. Same non-fatal contract as the
    Redis probe — log clearly, never crash the lifespan."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("[startup] postgres ping ok")
    except Exception as exc:  # noqa: BLE001 — never fatal at startup
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


class InsecureProductionConfig(RuntimeError):
    """Raised at startup when prod is booting with known-default secrets.

    Fail-closed beats fail-open: a forgotten env var is the single most
    common cause of "we shipped with the dev secret." Refusing to start
    surfaces the problem immediately instead of letting forgeable JWTs
    serve traffic.
    """


def _enforce_prod_secrets(settings) -> None:
    """Hard-fail boot if prod is using the dev default for any secret.
    Called from create_app() — must run BEFORE the app starts serving."""
    if settings.env not in ("prod", "production"):
        return

    failures: list[str] = []
    if settings.jwt_secret == _DEFAULT_JWT_SECRET:
        failures.append(
            "AGG_JWT_SECRET is the dev default. Generate: "
            "openssl rand -hex 32"
        )
    if not settings.webhook_secret:
        failures.append(
            "AGG_WEBHOOK_SECRET is unset. Set the same value as MDProject's "
            "AGGRIGATOR_WEBHOOK_SECRET so HMAC verification matches."
        )
    if not settings.webhook_target_url:
        failures.append(
            "AGG_WEBHOOK_TARGET_URL is unset. Point this at MDProject's "
            "inbound webhook endpoint, e.g. "
            "https://mdproject.example.com/sportgameodds/webhook."
        )
    if not failures:
        return
    msg = (
        "[startup] refusing to start in prod with default secrets:\n  - "
        + "\n  - ".join(failures)
    )
    logger.error(msg)
    raise InsecureProductionConfig(msg)


def _warn_on_misconfig(settings) -> None:
    """One-shot log audit at startup: surface the most expensive-if-missed
    misconfigs (default secrets in prod, missing host allowlist, etc.).

    Note: AGG_JWT_SECRET / AGG_WEBHOOK_SECRET / AGG_WEBHOOK_TARGET_URL are
    NOT checked here because they're already fatal in prod — see
    ``_enforce_prod_secrets``.
    """
    is_prod = settings.env in ("prod", "production")
    if not is_prod:
        return
    if not settings.allowed_hosts_list:
        logger.warning(
            "[startup] AGG_ALLOWED_HOSTS is empty in prod — Host-header "
            "spoofing is not blocked. Set to your Railway domain + any "
            "custom domains."
        )
    if not settings.odds_api_key:
        logger.warning(
            "[startup] ODDSAPI_API_KEY is unset in prod — every odds-api "
            "call will fail. Set the real key from your odds-api.io account."
        )
    if settings.test_mode:
        logger.warning(
            "[startup] AGG_TEST_MODE=true in prod — destructive endpoints "
            "(/ops/data-reset, /ops/ingest-event) are reachable. Disable "
            "before going public."
        )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Belt-and-suspenders: every probe + warning here is independently
    # try/except-ed already, but wrapping the lifespan body in a final
    # catch-all means a bug in the boot logic can never take the app
    # down. If something raises, gunicorn's worker stays up and we lose
    # only the diagnostic line — better than no service.
    procrastinate_opened = False
    try:
        settings = get_settings()
        logger.info(
            "[startup] aggrigator %s booting env=%s debug=%s log_level=%s",
            __version__, settings.env, settings.debug, settings.log_level,
        )
        _warn_on_misconfig(settings)
        await _probe_db()
        # Open the Procrastinate connection pool once per FastAPI process.
        # Required for ``task.defer_async()`` calls from the orchestrator +
        # watchdog (push-on-write webhook notifications). If this fails we
        # log and continue — defer_async will surface its own error if hit,
        # which is preferable to refusing to serve traffic.
        try:
            await procrastinate_app.open_async()
            procrastinate_opened = True
            logger.info("[startup] procrastinate app pool opened")
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[startup] procrastinate open_async FAILED %s: %s — "
                "task defers will error until this is fixed.",
                type(exc).__name__, exc,
            )
    except Exception as exc:  # noqa: BLE001 — never fail startup
        logger.exception("[startup] lifespan probe crashed: %s", exc)
    yield
    logger.info("[shutdown] aggrigator stopping")
    if procrastinate_opened:
        try:
            await procrastinate_app.close_async()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[shutdown] procrastinate close_async failed %s: %s",
                type(exc).__name__, exc,
            )


def create_app() -> FastAPI:
    settings = get_settings()
    is_prod = settings.env in ("prod", "production")

    configure_logging(settings.log_level)

    # Fail-closed on default secrets in prod — raises before the app
    # starts serving. Better an immediate boot-loop than a quietly
    # forgeable JWT.
    _enforce_prod_secrets(settings)

    # OpenAPI / Swagger / Redoc are recon material — they expose the full
    # route + schema graph to any unauthenticated probe. Default is OFF in
    # every environment; flip ``AGG_DOCS_ENABLED=true`` only on short-lived
    # dev/test environments where you actively need the docs UI. Safer
    # default than "on except in prod" — a misconfigured staging deploy
    # never leaks the schema.
    docs_enabled = settings.docs_enabled
    if docs_enabled and is_prod:
        # Defense-in-depth: refuse the combination. An operator who flips
        # this on for "5 minutes of debugging" would otherwise leak the
        # schema until they remember to flip it back.
        logger.error(
            "[startup] AGG_DOCS_ENABLED=true is forbidden in prod — "
            "force-disabling. /docs, /redoc, /openapi.json will return 404. "
            "If you need the docs UI, do it on a non-prod deploy."
        )
        docs_enabled = False
    docs_kwargs: dict = (
        {}
        if docs_enabled
        else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    )

    app = FastAPI(
        title="Aggrigator",
        version=__version__,
        lifespan=_lifespan,
        **docs_kwargs,
    )

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
        allow_headers=["Authorization", "Content-Type"],
    )
    # Session signing key. Prefer the dedicated AGG_SESSION_SECRET; fall
    # back to jwt_secret with a WARNING so existing prod deploys don't
    # break, but operators are nudged to split the keys.
    if settings.session_secret:
        session_signing_key = settings.session_secret
    else:
        session_signing_key = settings.jwt_secret
        if is_prod:
            logger.warning(
                "[startup] AGG_SESSION_SECRET unset — reusing AGG_JWT_SECRET "
                "for session cookies. Set a separate value so JWT and "
                "session can be rotated independently."
            )
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_signing_key,
        session_cookie="aggrigator_session",
        same_site="lax",
        https_only=is_prod,
    )
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
        # Minimal CSP. SQLAdmin uses inline scripts/styles, so a strict
        # ``script-src 'self'`` would break the admin UI. We instead lock
        # down the things we don't need — embedding (frame-ancestors),
        # base href injection (base-uri), form posts to other origins
        # (form-action). Together these blunt the impact of any future
        # template-XSS without coupling to SQLAdmin's internals.
        h["Content-Security-Policy"] = (
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        # HSTS only in prod — we're always behind Railway's HTTPS edge
        # there. In dev (HTTP localhost) HSTS would lock the browser to
        # https for the host and break local testing.
        if is_prod:
            h["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=_security_headers_dispatch)

    # Conditional-GET layer for analytics. Computes an ETag from the
    # response body hash and sets Cache-Control: private, max-age=300.
    # Clients (MDProject's AggrigatorClient, browsers, any HTTP cache)
    # can replay the ETag via If-None-Match and get a 304 when the data
    # hasn't changed — saves the JSON parse and the payload bytes.
    #
    # "private" because /v1/analytics requires a tenant API key — the
    # response varies per tenant and shouldn't be stored by any shared
    # intermediary. End-user browser cache + MDProject's per-process
    # cache both honor private. 5 min TTL is chosen to bracket the
    # "data only changes on event settle" expectation.
    async def _analytics_cache_dispatch(request, call_next):
        if request.method != "GET" or not request.url.path.startswith("/v1/analytics"):
            return await call_next(request)
        response = await call_next(request)
        if response.status_code != 200:
            return response
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        etag = '"' + hashlib.md5(body).hexdigest() + '"'
        cache_control = "private, max-age=300"
        if request.headers.get("if-none-match") == etag:
            return Response(
                status_code=304,
                headers={
                    "ETag": etag,
                    "Cache-Control": cache_control,
                },
            )
        headers = dict(response.headers)
        headers["ETag"] = etag
        headers["Cache-Control"] = cache_control
        # content-length will be re-derived from body length by Starlette.
        headers.pop("content-length", None)
        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )

    app.add_middleware(BaseHTTPMiddleware, dispatch=_analytics_cache_dispatch)

    app.include_router(auth_router.router)
    app.include_router(references_router.router)
    app.include_router(events_router.router)
    app.include_router(selections_router.router)
    app.include_router(analytics_router.router)
    app.include_router(bets_router.router)
    app.include_router(internal_router.router)
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
