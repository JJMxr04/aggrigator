"""HTML page at ``/ops/crons`` — server-rendered Jinja2 + HTMX.

**Admin-only.** Every route in this module calls ``_admin_from_session`` which
returns the User row only if (a) the SQLAdmin session cookie carries an
``admin_user_id``, (b) the user is active, and (c) ``user.role == 'admin'``.
If any check fails:

- The main ``/ops/crons`` page returns **302 to /admin** so the operator lands
  on the SQLAdmin login form instead of a raw 401.
- HTMX partials return **401** (the JS surfaces a toast and the user
  re-authenticates).

The HTML page builds on top of the JSON API at ``/v1/admin/crons/*`` — no
business logic in the templates; service-layer calls only. Replaces v0
``api/ops_console.py`` (plan §2.1.10).
"""

from __future__ import annotations

import hmac
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from redis.asyncio import Redis

from aggrigator.config import get_settings
from aggrigator.db import async_session_factory
from aggrigator.models.auth import User, UserRole
from aggrigator.ops.data_reset import (
    known_tables,
    list_table_info,
    truncate_table,
)
from aggrigator.ops.service import CronService, LockUnavailable, TriggerRejected

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/ops", tags=["ops"], include_in_schema=False)


def _redis() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


# ---- main page -------------------------------------------------------------


@router.get("/crons", response_class=HTMLResponse)
async def crons_page(request: Request):
    user = await _admin_from_session(request)
    if user is None:
        # Bounce to SQLAdmin login. After login, SQLAdmin's "next" handling
        # will return them here. Status 303 (see-other) is the right verb
        # for a GET that turns into a different GET — also avoids browser
        # cache hits if the user previously viewed the page as admin.
        return RedirectResponse(
            url=f"/admin/login?next={request.url.path}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

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
    await _require_csrf(request)

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
            except LockUnavailable as exc:
                # Redis lock died. service.trigger already wrote a FAILED
                # cron_run row + logged the underlying error. Return 503
                # so the HTMX swap shows a clear failure (not a generic
                # blank 500), and the operator knows to fix
                # AGG_REDIS_URL or the Redis plugin.
                logger.warning(
                    "trigger_run_html: lock unavailable for cron=%s: %s",
                    name, exc,
                )
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        "Cron lock subsystem unavailable — Redis is unreachable "
                        "from agg-web. Check AGG_REDIS_URL and the Redis plugin."
                    ),
                )
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


@router.get("/crons/{name}/runs/{run_id}", response_class=HTMLResponse)
async def run_detail_panel(
    request: Request, name: str, run_id: int,
) -> HTMLResponse:
    """Slide-out detail for one run — full error text + full summary JSON."""
    user = await _admin_from_session(request)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)

    async with async_session_factory() as session:
        redis = _redis()
        try:
            svc = CronService(session, redis)
            detail = await svc.get_run(run_id)
        finally:
            await redis.aclose()
    if detail is None or detail["cron_name"] != name:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    import json
    summary_pretty = (
        json.dumps(detail.get("summary"), indent=2, sort_keys=True)
        if detail.get("summary") else None
    )
    return templates.TemplateResponse(
        request, "_run_detail.html",
        {
            "detail": detail,
            "summary_pretty": summary_pretty,
        },
    )


# ---- data-reset (truncate-with-CASCADE) ------------------------------------
#
# These routes wipe entire tables. They are gated by ``AGG_TEST_MODE`` AND
# admin authentication AND CSRF AND a typed-name confirmation. In production
# (the default ``AGG_TEST_MODE=False``) they 403 even for valid admins —
# the operator must explicitly opt the deployment into testing mode via
# ``.env`` to use these.


from aggrigator.ops.testmode import require_test_mode as _require_test_mode  # noqa: E402


@router.get("/data-reset", response_class=HTMLResponse)
async def data_reset_page(request: Request):
    """List every known table + its row count + a Delete-all button.

    Each truncate goes through ``POST /ops/data-reset/{table}`` which checks
    a typed-name confirmation before issuing ``TRUNCATE ... CASCADE``."""
    _require_test_mode()
    user = await _admin_from_session(request)
    if user is None:
        return RedirectResponse(
            url=f"/admin/login?next={request.url.path}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    async with async_session_factory() as session:
        tables = await list_table_info(session)
    grouped: dict[str, list] = {}
    for t in tables:
        grouped.setdefault(t.section, []).append(t)
    return templates.TemplateResponse(
        request,
        "data_reset.html",
        {
            "actor_email": user.email,
            "csrf_token": _csrf_token(request),
            "grouped_tables": grouped,
            "section_order": ["Domain", "Webhooks", "Ops", "Auth", "Other"],
            "flash": request.query_params.get("flash"),
        },
    )


@router.post("/data-reset/{table}", response_class=HTMLResponse)
async def data_reset_truncate(request: Request, table: str):
    """Truncate one table CASCADE.

    Three confirmation gates:
      1. ``AGG_TEST_MODE=True`` (env-driven) — see ``_require_test_mode``.
      2. CSRF token (header / hidden form field) — see ``_require_csrf``.
      3. Typed confirmation — the form submits ``confirm`` which must equal
         the table name. Catches accidental clicks. Server-side check.
    """
    _require_test_mode()
    user = await _admin_from_session(request)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    await _require_csrf(request)

    if table not in known_tables():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown table")

    form = await request.form()
    confirm = (form.get("confirm") or "").strip()
    if confirm != table:
        # Confirmation mismatch — bounce back with a flash.
        return RedirectResponse(
            url=f"/ops/data-reset?flash=mismatch:{table}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    actor_ip = request.client.host if request.client else None
    async with async_session_factory() as session:
        rows_removed = await truncate_table(
            session,
            table=table,
            actor_user_id=user.id,
            actor_email=user.email,
            ip=actor_ip,
        )

    return RedirectResponse(
        url=f"/ops/data-reset?flash=ok:{table}:{rows_removed}",
        status_code=status.HTTP_303_SEE_OTHER,
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


async def _require_csrf(request: Request) -> None:
    expected = request.session.get("ops_csrf")
    # Prefer header (HTMX hx-post path), fall back to form field (vanilla
    # ``<form>`` submit — used by the data-reset confirmation modal).
    submitted = (
        request.headers.get("X-CSRF-Token")
        or request.headers.get("X-CSRFToken")
    )
    if not submitted and request.method == "POST":
        try:
            form = await request.form()
            submitted = form.get("csrf_token")
        except Exception:  # noqa: BLE001
            submitted = None
    # Constant-time compare — protects against timing-based token recovery
    # (Starlette session is signed so the attack surface is narrow, but
    # str-eq leaks character-level timing in principle).
    if not expected or not submitted or not hmac.compare_digest(
        expected, submitted,
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or mismatch")
