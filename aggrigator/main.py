"""FastAPI app factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from aggrigator import __version__
from aggrigator.admin.views import mount_admin
from aggrigator.api import api_keys as api_keys_router
from aggrigator.api import auth as auth_router
from aggrigator.api import events as events_router
from aggrigator.api import references as references_router
from aggrigator.api import selections as selections_router
from aggrigator.api import webhook_endpoints as webhook_endpoints_router
from aggrigator.config import get_settings
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


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield


def create_app() -> FastAPI:
    settings = get_settings()

    configure_logging(settings.log_level)
    init_sentry(settings)

    app = FastAPI(
        title="Aggrigator",
        version=__version__,
        lifespan=_lifespan,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)

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
        https_only=settings.env in ("prod", "production"),
    )
    app.add_middleware(BaseHTTPMiddleware, dispatch=resolve_first_party_promotion)

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

    @app.get("/", include_in_schema=False)
    def index():
        return {
            "service": "aggrigator",
            "version": __version__,
            "docs": "/docs",
            "admin": "/admin",
            "ops": "/ops/crons",
            "healthz": "/healthz",
        }

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/readyz")
    def readyz():
        return {"ok": True}

    return app


app = create_app()
