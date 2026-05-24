"""``/v1/admin/crons/*`` — JSON API for the cron-runner.

Replaces ``api/admin_crons.py``. Same admin-only auth, same dep chain
(``require_admin`` → ``require_user`` → ``current_user``).

Endpoints (plan §2.1.5):
- ``GET  /v1/admin/crons``                           list every registered cron + last_run
- ``GET  /v1/admin/crons/{name}``                    one cron + last_run (HTMX poll target)
- ``POST /v1/admin/crons/{name}/run``                trigger; 409 if a run is in flight
- ``GET  /v1/admin/crons/{name}/runs``               run history (newest first)
- ``GET  /v1/admin/crons/{name}/runs/{id}``          full detail (untruncated error + summary)

The single-event ingest endpoint from v0 (`/v1/admin/crons/ingest-event`) is
preserved here for parity with the v0 surface — it doesn't go through the
cron_run recording layer because it's an ad-hoc one-off, not a scheduled job.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from aggrigator.config import get_settings
from aggrigator.deps import SessionDep, require_admin
from aggrigator.ingest.orchestrator import ingest_event as _ingest_event
from aggrigator.models import League, User
from aggrigator.ops.registry import REGISTRY
from aggrigator.ops.service import CronService, TriggerRejected
from aggrigator.workers.tasks.ingest import _build_client

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/admin/crons",
    tags=["admin: crons"],
    dependencies=[Depends(require_admin)],
)


# ---- list / detail ---------------------------------------------------------


@router.get("", summary="List every registered cron + last_run")
async def list_crons(session: SessionDep) -> list[dict]:
    svc = CronService(session)
    items = await svc.list_crons()
    return [_item_to_dict(it) for it in items]


@router.get("/{name}", summary="One cron + last_run (HTMX poll target)")
async def get_cron(name: str, session: SessionDep) -> dict:
    svc = CronService(session)
    item = await svc.get_cron(name)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cron not found")
    return _item_to_dict(item)


@router.get("/{name}/runs", summary="Run history (newest first)")
async def list_runs(
    name: str, session: SessionDep,
    limit: int = Query(default=25, ge=1, le=100),
) -> list[dict]:
    if not _is_registered(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cron not found")
    svc = CronService(session)
    return [_run_to_dict(r) for r in await svc.history(name, limit=limit)]


@router.get("/{name}/runs/{run_id}", summary="Full run detail (untruncated)")
async def get_run(
    name: str, run_id: int, session: SessionDep,
) -> dict:
    if not _is_registered(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cron not found")
    svc = CronService(session)
    body = await svc.get_run(run_id)
    if body is None or body["cron_name"] != name:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return _detail_to_dict(body)


# ---- trigger ---------------------------------------------------------------


@router.post("/{name}/run", summary="Trigger a manual run")
async def trigger_run(
    name: str,
    session: SessionDep,
    user: Annotated[User, Depends(require_admin)],
) -> dict:
    if not _is_registered(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cron not found")
    svc = CronService(session)
    try:
        run = await svc.trigger(name, actor=user)
    except TriggerRejected as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return _run_to_dict(run)


# ---- enable / disable (pause scheduled tick) -------------------------------


class CronEnabledIn(BaseModel):
    enabled: bool = Field(
        ...,
        description=(
            "True = scheduled tick runs as normal. False = scheduler "
            "skips this cron. Manual /run still works either way."
        ),
    )


@router.post(
    "/{name}/enabled",
    summary="Pause or resume the scheduled tick of a cron",
)
async def set_enabled(
    name: str,
    payload: CronEnabledIn,
    session: SessionDep,
    user: Annotated[User, Depends(require_admin)],
) -> dict:
    if not _is_registered(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cron not found")
    svc = CronService(session)
    try:
        item = await svc.set_enabled(name, enabled=payload.enabled, actor=user)
    except ValueError as exc:
        # Manual-only cron — nothing to pause.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return _item_to_dict(item)


# ---- single-event ingest (carried over from v0 — ad-hoc, not cron-recorded) ---


class IngestEventIn(BaseModel):
    event_id: str
    league_id: str | None = None


@router.post("/ingest-event", summary="Ingest one specific event by the provider eventID")
async def trigger_ingest_event(
    payload: IngestEventIn,
    session: SessionDep,
    _admin: Annotated[User, Depends(require_admin)],
) -> dict:
    """Ad-hoc one-event ingest. Useful when testing the lifecycle for a
    specific event without waiting on the league sweep.

    Gated by ``AGG_TEST_MODE`` — manual ingest in production would let an
    operator silently rewrite event state, so we keep it admin-only AND
    test-mode-only."""
    from aggrigator.ops.testmode import require_test_mode
    require_test_mode()
    client = _build_client()

    payload_dict: dict | None = None
    if payload.league_id:
        for ev in client.get_events(league_id=payload.league_id, event_id=payload.event_id):
            if ev.get("eventID") == payload.event_id:
                payload_dict = ev
                break
    else:
        leagues = list(await session.scalars(
            select(League).where(League.active.is_(True))
        ))
        for league in leagues:
            for ev in client.get_events(league_id=league.id, event_id=payload.event_id):
                if ev.get("eventID") == payload.event_id:
                    payload_dict = ev
                    break
            if payload_dict is not None:
                break

    if payload_dict is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"event {payload.event_id} not found in any active league",
        )

    result = await _ingest_event(session, payload_dict)
    await session.commit()
    if result is None:
        return {"ingested": False, "reason": "non-match event or league not seeded"}
    return {
        "ingested": True,
        "event_id": result.event.id,
        "transition": result.transition.value,
        "selections_written": result.selections_written,
        "selections_graded": result.selections_graded,    # PROVIDER
        "selections_computed": result.selections_computed,  # COMPUTED fallback
        "selections_voided": result.selections_voided,    # PENDING → VOID lock-in
        "deliveries_enqueued": result.deliveries_enqueued,
    }


# ---- historical ingest (TheSportsDB) ---------------------------------------


class HistoricalIngestIn(BaseModel):
    league_id: str
    season_label: str  # TheSportsDB strSeason; EPL "2024-2025", MLS "2024"


@router.post(
    "/historical-ingest",
    summary=(
        "Backfill historical events + final scores for one league, one "
        "season, from TheSportsDB. No markets / odds are written."
    ),
)
async def trigger_historical_ingest(
    payload: HistoricalIngestIn,
    session: SessionDep,
    _admin: Annotated[User, Depends(require_admin)],
) -> dict:
    """One league + one season per call. Refuses any league where
    ``can_pull_historical_scores=false`` (gate enforced in the
    orchestrator via :class:`HistoricalIngestError`).

    Runs synchronously and returns the report — for a full EPL season
    (38 rounds × 1.5s throttle) expect ~5 minutes. Future improvement:
    defer via Procrastinate if runs exceed admin-request timeouts.

    Idempotent on re-run: same TheSportsDB event id → same
    ``"tsdb:<id>"`` event_id → same upsert → no duplicate rows.
    """
    import dataclasses

    from aggrigator.ingest.historical_orchestrator import (
        HistoricalIngestError,
        ingest_historical_league,
    )
    from aggrigator.ingest.thesportsdb_client import (
        TheSportsDbClient,
        TheSportsDbError,
    )

    if not payload.season_label.strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "season_label must be non-empty (e.g. '2024-2025' or '2024')",
        )

    client = TheSportsDbClient(api_key=get_settings().thesportsdb_api_key)

    try:
        async with client:
            report = await ingest_historical_league(
                session, payload.league_id, payload.season_label,
                client=client,
            )
    except HistoricalIngestError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            str(exc),
        ) from exc
    except TheSportsDbError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"TheSportsDB error: {exc}",
        ) from exc

    return dataclasses.asdict(report)


# ---- helpers ---------------------------------------------------------------


def _is_registered(name: str) -> bool:
    return any(s.name == name for s in REGISTRY)


def _item_to_dict(it) -> dict:
    return {
        "name": it.name,
        "description": it.description,
        "schedule_human": it.schedule_human,
        "is_running": it.is_running,
        "is_scheduled": it.is_scheduled,
        "enabled": it.enabled,
        "last_run": _run_to_dict(it.last_run) if it.last_run else None,
    }


def _run_to_dict(r) -> dict:
    return {
        "id": r.id, "cron_name": r.cron_name,
        "trigger_source": r.trigger_source,
        "started_by_email": r.started_by_email,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "status": r.status,
        "duration_seconds": r.duration_seconds,
        "items_processed": r.items_processed,
        "error_excerpt": r.error_excerpt,
    }


def _detail_to_dict(d: dict) -> dict:
    return {
        **_run_to_dict(type("R", (), d)()),
        "summary": d.get("summary"),
        "error_full": d.get("error_full"),
    }
