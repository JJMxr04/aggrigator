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
from pydantic import BaseModel
from redis.asyncio import Redis
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


def _redis() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


# ---- list / detail ---------------------------------------------------------


@router.get("", summary="List every registered cron + last_run")
async def list_crons(session: SessionDep) -> list[dict]:
    redis = _redis()
    try:
        svc = CronService(session, redis)
        items = await svc.list_crons()
        return [_item_to_dict(it) for it in items]
    finally:
        await redis.aclose()


@router.get("/{name}", summary="One cron + last_run (HTMX poll target)")
async def get_cron(name: str, session: SessionDep) -> dict:
    redis = _redis()
    try:
        svc = CronService(session, redis)
        item = await svc.get_cron(name)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "cron not found")
        return _item_to_dict(item)
    finally:
        await redis.aclose()


@router.get("/{name}/runs", summary="Run history (newest first)")
async def list_runs(
    name: str, session: SessionDep,
    limit: int = Query(default=25, ge=1, le=100),
) -> list[dict]:
    if not _is_registered(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cron not found")
    redis = _redis()
    try:
        svc = CronService(session, redis)
        return [_run_to_dict(r) for r in await svc.history(name, limit=limit)]
    finally:
        await redis.aclose()


@router.get("/{name}/runs/{run_id}", summary="Full run detail (untruncated)")
async def get_run(
    name: str, run_id: int, session: SessionDep,
) -> dict:
    if not _is_registered(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cron not found")
    redis = _redis()
    try:
        svc = CronService(session, redis)
        body = await svc.get_run(run_id)
        if body is None or body["cron_name"] != name:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
        return _detail_to_dict(body)
    finally:
        await redis.aclose()


# ---- trigger ---------------------------------------------------------------


@router.post("/{name}/run", summary="Trigger a manual run")
async def trigger_run(
    name: str,
    session: SessionDep,
    user: Annotated[User, Depends(require_admin)],
) -> dict:
    if not _is_registered(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cron not found")
    redis = _redis()
    try:
        svc = CronService(session, redis)
        try:
            run = await svc.trigger(name, actor=user)
        except TriggerRejected as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
        return _run_to_dict(run)
    finally:
        await redis.aclose()


# ---- single-event ingest (carried over from v0 — ad-hoc, not cron-recorded) ---


class IngestEventIn(BaseModel):
    event_id: str
    league_id: str | None = None


@router.post("/ingest-event", summary="Ingest one specific event by SGO eventID")
async def trigger_ingest_event(
    payload: IngestEventIn,
    session: SessionDep,
    _admin: Annotated[User, Depends(require_admin)],
) -> dict:
    """Ad-hoc one-event ingest. Useful when testing the lifecycle for a
    specific event without waiting on the league sweep."""
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


# ---- helpers ---------------------------------------------------------------


def _is_registered(name: str) -> bool:
    return any(s.name == name for s in REGISTRY)


def _item_to_dict(it) -> dict:
    return {
        "name": it.name,
        "description": it.description,
        "schedule_human": it.schedule_human,
        "is_running": it.is_running,
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
