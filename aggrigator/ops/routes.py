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
business logic in the templates; service-layer calls only.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from enum import Enum
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from sqlalchemy import select

from aggrigator.db import async_session_factory
from aggrigator.models import League
from aggrigator.models.auth import User, UserRole
from aggrigator.ops.data_reset import (
    known_tables,
    list_table_info,
    truncate_table,
)
from aggrigator.ops.service import CronService, TriggerRejected

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/ops", tags=["ops"], include_in_schema=False)


# ---- main page -------------------------------------------------------------


@router.get("/crons", response_class=HTMLResponse)
async def crons_page(request: Request):
    user = await _admin_from_session(request)
    if user is None:
        return RedirectResponse(
            url=f"/admin/login?next={request.url.path}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    async with async_session_factory() as session:
        svc = CronService(session)
        items = await svc.list_crons()

    return templates.TemplateResponse(
        request,
        "crons_index.html",
        {
            "items": items,
            "actor_email": user.email,
            "csrf_token": _csrf_token(request),
        },
    )


# ---- logo backfill ---------------------------------------------------------


@router.get("/logo-backfill", response_class=HTMLResponse)
async def logo_backfill_page(request: Request):
    user = await _admin_from_session(request)
    if user is None:
        return RedirectResponse(
            url=f"/admin/login?next={request.url.path}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    async with async_session_factory() as session:
        leagues = list(
            await session.scalars(
                select(League).where(League.active.is_(True)).order_by(League.name)
            )
        )
    return templates.TemplateResponse(
        request,
        "logo_backfill.html",
        {
            "leagues": leagues,
            "actor_email": user.email,
            "csrf_token": _csrf_token(request),
        },
    )


@router.post("/logo-backfill/run", response_class=HTMLResponse)
async def logo_backfill_run(request: Request) -> HTMLResponse:
    """Manual crest fetch. Runs inline (same as a manual cron trigger) and
    returns the result partial that HTMX swaps into ``#lb-result``."""
    user = await _admin_from_session(request)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    await _require_csrf(request)

    form = await request.form()
    league_id = (form.get("league_id") or "").strip() or None
    retry_missing = (form.get("retry_missing") or "").strip().lower() == "true"
    force = (form.get("force") or "").strip().lower() == "true"

    # Lazy import — the workers package pulls in the Procrastinate app; importing
    # it at module load would couple the web process boot to the worker wiring.
    from aggrigator.workers.tasks.logos import run_backfill_team_logos

    summary = await run_backfill_team_logos(
        league_id=league_id, retry_missing=retry_missing, force=force
    )
    return templates.TemplateResponse(
        request, "_logo_backfill_result.html", {"summary": summary},
    )


# ---- HTMX partials ---------------------------------------------------------


@router.get("/crons/{name}/row", response_class=HTMLResponse)
async def cron_row(request: Request, name: str) -> HTMLResponse:
    """Single row partial — used by manual refresh after a Run-now click."""
    user = await _admin_from_session(request)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)

    async with async_session_factory() as session:
        svc = CronService(session)
        item = await svc.get_cron(name)
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
        svc = CronService(session)
        try:
            await svc.trigger(name, actor=user)
        except TriggerRejected:
            # 409-equivalent — already running. The row partial below
            # will render the in-flight state from the cron_run row.
            pass
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        item = await svc.get_cron(name)
    return templates.TemplateResponse(
        request, "_cron_row.html",
        {"item": item, "csrf_token": _csrf_token(request)},
    )


@router.post("/crons/{name}/toggle", response_class=HTMLResponse)
async def toggle_enabled_html(request: Request, name: str) -> HTMLResponse:
    """Pause / resume the scheduled tick. Returns the updated row partial
    so the toggle UI reflects the new state without a full page reload.

    The form submits ``enabled=true|false`` (the desired *new* state).
    Manual ``Run now`` continues to work regardless — disabling only
    stops the periodic scheduled firing."""
    user = await _admin_from_session(request)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    await _require_csrf(request)

    form = await request.form()
    raw = (form.get("enabled") or "").strip().lower()
    if raw not in ("true", "false"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "enabled must be 'true' or 'false'",
        )
    enabled = raw == "true"

    async with async_session_factory() as session:
        svc = CronService(session)
        try:
            item = await svc.set_enabled(name, enabled=enabled, actor=user)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
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
        svc = CronService(session)
        runs = await svc.history(name, limit=25)
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
        svc = CronService(session)
        detail = await svc.get_run(run_id)
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


# (The historical-ingest form + helpers were removed in Phase 16 — TheSportsDB
# backfill is retired; odds-api is the sole source. The ``historical_ingest.html``
# template is left unused but harmless.)


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


# Allowlist in the SIGNATURE: the path param is typed as an
# Enum built from SQLAlchemy metadata, so FastAPI 422s anything outside
# ``known_tables()`` before the handler runs. The body re-check below is
# retained as belt-and-suspenders.
KnownTable = Enum("KnownTable", {name: name for name in known_tables()})


@router.post("/data-reset/{table}", response_class=HTMLResponse)
async def data_reset_truncate(request: Request, table: KnownTable):
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

    table = table.value
    if table not in known_tables():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown table")

    form = await request.form()
    confirm = (form.get("confirm") or "").strip()
    if confirm != table:
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


# ---- error-tracking test (GlitchTip) ---------------------------------------


class GlitchTipTestError(RuntimeError):
    """Deliberately raised by ``POST /ops/sentry-test`` — never a real failure.

    Distinct class so the event is unmistakable in the GlitchTip issue list
    and groups separately from genuine errors."""


@router.post("/sentry-test")
async def sentry_test_throw(request: Request):
    """Raise an unhandled exception on purpose to verify error tracking.

    Exercises the *real* path — route → Starlette → sentry-sdk →
    GlitchTip — rather than a hand-rolled ``capture_message``, so it also
    proves the integration's middleware hooks are wired. The browser gets
    a genuine 500.

    Admin + CSRF gated, but deliberately NOT test-mode gated: verifying
    error tracking right after a production deploy is legitimate. The
    button lives on /ops/crons, so authed admins can trigger it in prod
    too. With ``AGG_SENTRY_DSN`` unset the exception still raises — it
    just isn't reported anywhere.
    """
    user = await _admin_from_session(request)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    await _require_csrf(request)

    logger.warning("[sentry-test] deliberate test error triggered by %s", user.email)
    raise GlitchTipTestError(
        f"GlitchTip test event — deliberately triggered by {user.email}"
        " via POST /ops/sentry-test"
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
    """Mint a CSRF token bound to the current session."""
    token = request.session.get("ops_csrf")
    if not token:
        token = uuid.uuid4().hex
        request.session["ops_csrf"] = token
    return token


async def _require_csrf(request: Request) -> None:
    expected = request.session.get("ops_csrf")
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
    if not expected or not submitted or not hmac.compare_digest(
        expected, submitted,
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or mismatch")
