"""Vacuum task — delete terminal events older than ``AGG_VACUUM_DAYS``.

Runs nightly @ 04:00 UTC by default (after ``settle_pending`` finishes at
03:30, so we never delete an event that's still in the settle queue).

Why this exists: free Postgres tiers (Neon at 0.5 GB) fill up fast when
the worker is ingesting events 24/7 and never pruning. A finished game's
markets + selections + quotes + webhook_delivery rows are no longer
read by anything after a couple of days, so we drop them.

Cascade behavior is handled at the **DB layer**, not the ORM:

- ``core_market.event_id`` → ``ON DELETE CASCADE``
- ``core_selection.market_id`` → ``ON DELETE CASCADE``
- ``core_quote.selection_id`` → ``ON DELETE CASCADE``
- ``webhook_delivery.event_id`` → ``ON DELETE CASCADE``

So one ``DELETE FROM core_event_event WHERE id IN (...)`` removes
events + every dependent row in a single transaction.

Set ``AGG_VACUUM_DAYS=0`` to disable (each run becomes a no-op but the
cron stays registered + visible in /ops/crons).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import or_, select

from aggrigator.config import get_settings
from aggrigator.db import session_scope
from aggrigator.ingest.lifecycle import TERMINAL_STATUSES
from aggrigator.models.event import Event
from aggrigator.ops.recorder import cron_run_recorder
from aggrigator.workers.app import app

logger = logging.getLogger(__name__)


async def run_vacuum_old_events() -> dict[str, Any]:
    """Delete up to ``AGG_VACUUM_BATCH_SIZE`` terminal events whose
    ``start_time`` is more than ``AGG_VACUUM_DAYS`` in the past.

    Returns a summary dict the cron_run row records. Re-running is safe
    and idempotent — each call processes one batch, so a backlog drains
    over multiple cron fires.
    """
    settings = get_settings()
    days = settings.vacuum_days
    batch = max(1, settings.vacuum_batch_size)

    if days <= 0:
        logger.info("vacuum: disabled (AGG_VACUUM_DAYS=%s)", days)
        return {
            "events_deleted": 0,
            "skipped": "disabled (AGG_VACUUM_DAYS<=0)",
            "days_old_threshold": days,
        }

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)

    async with session_scope() as session:
        # Select first, delete second — keeps the transaction predictable
        # and gives us a row count without relying on RETURNING (which
        # SQLAlchemy + asyncpg handle but adds noise on bulk DELETE).
        ids = list(
            await session.scalars(
                select(Event.id)
                .where(
                    Event.start_time.is_not(None),
                    Event.start_time < cutoff,
                    or_(
                        Event.is_finalized.is_(True),
                        Event.status_type.in_(TERMINAL_STATUSES),
                    ),
                )
                .limit(batch)
            )
        )
        if not ids:
            logger.info(
                "vacuum: nothing to delete (cutoff=%s days_old=%s)",
                cutoff.isoformat(), days,
            )
            return {
                "events_deleted": 0,
                "days_old_threshold": days,
                "cutoff_iso": cutoff.isoformat(),
            }

        # synchronize_session=False because we don't care about ORM state
        # afterward — the session is dropped at end-of-scope. DB-level FK
        # cascades handle markets / selections / quotes / webhook_delivery.
        result = await session.execute(
            sa_delete(Event)
            .where(Event.id.in_(ids))
            .execution_options(synchronize_session=False)
        )
        deleted = result.rowcount or 0

    logger.info(
        "vacuum: deleted %s events (cutoff=%s days_old=%s batch_limit=%s)",
        deleted, cutoff.isoformat(), days, batch,
    )
    return {
        "events_deleted": deleted,
        "days_old_threshold": days,
        "batch_limit": batch,
        "cutoff_iso": cutoff.isoformat(),
    }


# ---- Procrastinate entry point --------------------------------------------


@app.periodic(cron="0 4 * * *")
@app.task(
    name="aggrigator.vacuum_old_events",
    pass_context=True,
    queueing_lock="vacuum_old_events",
)
@cron_run_recorder("vacuum_old_events")
async def vacuum_old_events_task(context, timestamp: int | None = None) -> dict:
    return await run_vacuum_old_events()
