"""HTML page at ``/ops/crons`` — server-rendered Jinja2 + HTMX.

Auth gate: SQLAdmin **session cookie** with ``admin_user_id`` AND
``user.role == 'admin'``. Same gate the v0 page used. The HTML page builds on
top of the JSON API at ``/v1/admin/crons/*`` — no business logic in the
templates; service-layer calls only.

Replaces ``api/ops_console.py`` (plan §2.1.10).
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from redis.asyncio import Redis

from aggrigator.config import get_settings
from aggrigator.db import async_session_factory
from aggrigator.models.auth import User, UserRole
from aggrigator.ops.service import CronService, TriggerRejected

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/ops", tags=["ops"], include_in_schema=False)


def _redis() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


# ---- main page -------------------------------------------------------------


@router.get("/crons", response_class=HTMLResponse)
async def crons_page(request: Request) -> HTMLResponse:
    user = await _admin_from_session(request)
    if user is None:
        return _login_redirect()

    async with async_session_factory() as session:
        redis = _redis()
        try:
            svc = CronService(session, redis)
            items = await svc.list_crons()
        finally:
            await redis.aclose()

    return templates.TemplateResponse(
        request,
        "crons_index.html",
        {
            "items": items,
            "actor_email": user.email,
            "csrf_token": _csrf_token(request),
        },
    )


# ---- HTMX partials ---------------------------------------------------------


@router.get("/crons/{name}/row", response_class=HTMLResponse)
async def cron_row(request: Request, name: str) -> HTMLResponse:
    """Single row partial — HTMX polls this every 2s while a run is in flight."""
    user = await _admin_from_session(request)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)

    async with async_session_factory() as session:
        redis = _redis()
        try:
            svc = CronService(session, redis)
            item = await svc.get_cron(name)
        finally:
            await redis.aclose()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        request, "_cron_row.html",
        {"item": item, "csrf_token": _csrf_token(request)},
    )


@router.post("/crons/{name}/run", response_class=HTMLResponse)
async def trigger_run_html(request: Request, name: str) -> HTMLResponse:
    """HTML-form trigger — same auth + lock as the JSON API; returns the row partial."""
    user = await _admin_from_session(request)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    _require_csrf(request)

    async with async_session_factory() as session:
        redis = _redis()
        try:
            svc = CronService(session, redis)
            try:
                await svc.trigger(name, actor=user)
            except TriggerRejected:
                # Lock collision — render the row anyway (the UI shows
                # "running" because the existing row is still in flight).
                pass
            except KeyError:
                raise HTTPException(status.HTTP_404_NOT_FOUND)
            item = await svc.get_cron(name)
        finally:
            await redis.aclose()
    return templates.TemplateResponse(
        request, "_cron_row.html",
        {"item": item, "csrf_token": _csrf_token(request)},
    )


@router.get("/crons/{name}/history", response_class=HTMLResponse)
async def history_panel(request: Request, name: str) -> HTMLResponse:
    user = await _admin_from_session(request)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)

    async with async_session_factory() as session:
        redis = _redis()
        try:
            svc = CronService(session, redis)
            runs = await svc.history(name, limit=25)
        finally:
            await redis.aclose()
    return templates.TemplateResponse(
        request, "_run_panel.html",
        {"name": name, "runs": runs},
    )


# ---- helpers ---------------------------------------------------------------


async def _admin_from_session(request: Request) -> User | None:
    user_id = request.session.get("admin_user_id")
    if not user_id:
        return None
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        return None
    async with async_session_factory() as session:
        user = await session.get(User, uid)
    if user is None or not user.is_active or user.role != UserRole.ADMIN:
        return None
    return user


def _login_redirect() -> HTMLResponse:
    return HTMLResponse(
        '<p>Not signed in. <a href="/admin">Log in to SQLAdmin</a> first, '
        'then come back to <a href="/ops/crons">/ops/crons</a>.</p>',
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


def _csrf_token(request: Request) -> str:
    """Mint a CSRF token bound to the current session.

    Cheap impl: store the token on the session itself; the form posts it back
    in a hidden field. ``itsdangerous`` is overkill here since Starlette's
    SessionMiddleware already signs the cookie — anything written to
    ``request.session`` is integrity-checked end to end.
    """
    token = request.session.get("ops_csrf")
    if not token:
        token = uuid.uuid4().hex
        request.session["ops_csrf"] = token
    return token


def _require_csrf(request: Request) -> None:
    expected = request.session.get("ops_csrf")
    # Prefer header; fall back to form field (HTMX hx-post sends the form).
    submitted = request.headers.get("X-CSRF-Token")
    if not submitted:
        # FastAPI doesn't materialize form data here; check headers.x-csrftoken
        # before failing — HTMX can be configured to send either name.
        submitted = request.headers.get("X-CSRFToken")
    if not expected or not submitted or expected != submitted:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or mismatch")
