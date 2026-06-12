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
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from aggrigator.db import async_session_factory
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


# ---- cost estimate helper (historical ingest) ------------------------------


def _cost_estimate_context(leagues) -> dict:
    """Build template variables for the historical-ingest form.

    The free TheSportsDB tier doesn't bill, so there's no credit
    estimate. We surface ``rounds`` (an upper bound used for wall-clock
    estimation: 1.5s throttle × rounds) per league so the operator
    has *some* sense of how long the run will take.
    """
    # TheSportsDB league round upper bounds. EPL is 38; MLS has ~50
    # including playoffs. Anything else is best-guess 38.
    LEAGUE_ROUND_UPPER = {"EPL": 38, "MLS": 55}
    estimates: dict[str, dict] = {}
    for lg in leagues:
        rounds = LEAGUE_ROUND_UPPER.get(lg.id, 38)
        estimates[lg.id] = {
            "rounds": rounds,
            "approx_seconds": int(rounds * 1.5),
            "season_format": _season_format(lg.id),
        }
    return {"estimates": estimates}


# Leagues whose TheSportsDB ``strSeason`` spans two calendar years (a
# fall→spring season, e.g. "2024-2025"). Everything else runs inside a single
# calendar year, so its season label is a bare year (e.g. "2024").
_TWO_YEAR_SEASON_LEAGUES = {"EPL", "NBA", "NHL", "NFL", "UEFA_CHAMPIONS_LEAGUE"}


def _season_format(league_id: str) -> dict[str, str]:
    """Season-label (TheSportsDB ``strSeason``) format hint for one league,
    surfaced in the historical-ingest reference table so operators type the
    right thing into the free-text Season label box."""
    if league_id in _TWO_YEAR_SEASON_LEAGUES:
        return {"pattern": "YYYY-YYYY", "example": "2024-2025"}
    return {"pattern": "YYYY", "example": "2024"}


# ---- historical ingest (TheSportsDB) ---------------------------------------


@router.get("/historical-ingest", response_class=HTMLResponse)
async def historical_ingest_page(request: Request):
    """Form page: pick one eligible league + season label, then run
    backfill from TheSportsDB. Admin-only (same gate as /ops/crons)."""
    user = await _admin_from_session(request)
    if user is None:
        return RedirectResponse(
            url=f"/admin/login?next={request.url.path}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    from sqlalchemy import select as _select
    from aggrigator.models import League as _League

    async with async_session_factory() as session:
        leagues = list(await session.scalars(
            _select(_League)
            .where(
                _League.active.is_(True),
                _League.can_pull_historical_scores.is_(True),
            )
            .order_by(_League.id)
        ))

    return templates.TemplateResponse(
        request, "historical_ingest.html",
        {
            "actor_email": user.email,
            "csrf_token": _csrf_token(request),
            "leagues": leagues,
            "default_season_label": "",
            "report": None,
            "flash": request.query_params.get("flash"),
            **_cost_estimate_context(leagues),
        },
    )


@router.post("/historical-ingest", response_class=HTMLResponse)
async def historical_ingest_run(request: Request):
    """Run the historical-scores ingest synchronously and render the
    report inline.

    Single league + single season per submission. The orchestrator
    commits per-round so a mid-walk failure doesn't roll back what's
    been ingested. Re-runs are idempotent.
    """
    import dataclasses

    user = await _admin_from_session(request)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    await _require_csrf(request)

    form = await request.form()
    league_id = (form.get("league_id") or "").strip()
    season_label = (form.get("season_label") or "").strip()
    if not season_label:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "season_label must be non-empty (e.g. '2024-2025' or '2024')",
        )

    from aggrigator.config import get_settings
    from aggrigator.ingest.historical_orchestrator import (
        HistoricalIngestError,
        ingest_historical_league,
    )
    from aggrigator.ingest.thesportsdb_client import (
        TheSportsDbClient,
        TheSportsDbError,
    )
    from sqlalchemy import select as _select
    from aggrigator.models import League as _League

    flash: str | None = None
    report_dict: dict | None = None
    client = TheSportsDbClient(api_key=get_settings().thesportsdb_api_key)

    try:
        async with client:
            async with async_session_factory() as session:
                report = await ingest_historical_league(
                    session, league_id, season_label, client=client,
                )
        report_dict = dataclasses.asdict(report)
    except HistoricalIngestError as exc:
        flash = f"refused: {exc}"
    except TheSportsDbError as exc:
        flash = f"TheSportsDB error: {exc}"

    # Re-fetch the eligible-league list so the form re-renders with the
    # same dropdown after the POST.
    async with async_session_factory() as session:
        leagues = list(await session.scalars(
            _select(_League)
            .where(
                _League.active.is_(True),
                _League.can_pull_historical_scores.is_(True),
            )
            .order_by(_League.id)
        ))

    return templates.TemplateResponse(
        request, "historical_ingest.html",
        {
            "actor_email": user.email,
            "csrf_token": _csrf_token(request),
            "leagues": leagues,
            "default_season_label": season_label,
            "report": report_dict,
            "flash": flash,
            **_cost_estimate_context(leagues),
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
    error tracking right after a production deploy is legitimate. (The
    button lives on /ops/data-reset, which IS test-mode-only, so in prod
    this is reachable only by hand-crafted request from an authed admin.)
    With ``AGG_SENTRY_DSN`` unset the exception still raises — it just
    isn't reported anywhere.
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
