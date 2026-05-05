"""Lifecycle watchdog — find and (optionally) recover stale events.

Background: the rest of the aggregator faithfully mirrors SGO. If SGO
keeps an event marked ``notstarted`` long after its scheduled start
(postponed game upstream never re-statused, feed dropped it, etc.), the
local row sits forever in ``notstarted`` and any PENDING selections never
resolve — they get hard-deleted by ``vacuum`` 3 days later with no
``event.voided`` webhook ever firing. Subscribers see nothing.

This module fixes that. Two functions:

- :func:`scan_stale_events` — informational. Returns the IDs/count of
  events past ``stale_grace_hours`` still in ``notstarted`` /
  ``inprogress``. No DB writes. Surfaces in /ops + the API ``stale``
  flag.

- :func:`auto_void_stale_events` — opt-in (env: ``AGG_LIFECYCLE_AUTO_VOID_HOURS``,
  default 0 = disabled). For events past the longer auto-void threshold,
  flips ``status_type=canceled`` + ``feed_locked=True``, voids all
  PENDING selections (reusing :func:`void_remaining_pending`), and fires
  ``event.voided`` via the standard enqueue path.

Recovery: if SGO eventually returns the event with real scores, the next
lifecycle pass sees ``canceled → finished``, fires FINALIZED, and
PROVIDER grading overrides the COMPUTED-source VOID rows (per
``SOURCE_PRIORITY`` in ``settlement_computed.py``). So an over-eager
auto-void self-corrects when the upstream truth shows up.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.ingest.lifecycle import Transition
from aggrigator.models import Event, Market, Selection
from aggrigator.webhooks.enqueue import enqueue_for_event

logger = logging.getLogger(__name__)


# Mirror the predicate in compute_stale — these are the statuses the
# watchdog considers "should be over by now if start_time is in the past."
_STUCK_STATUSES: tuple[str, ...] = ("notstarted", "inprogress")


class WatchdogReport(NamedTuple):
    stale_count: int
    stale_event_ids: list[str]
    voided_count: int
    voided_event_ids: list[str]
    selections_voided: int
    deliveries_enqueued: int


async def scan_stale_events(
    session: AsyncSession,
    *,
    grace_hours: int,
    now: datetime | None = None,
    limit: int = 500,
) -> list[Event]:
    """Return Event rows past ``grace_hours`` still in ``notstarted`` /
    ``inprogress``. No mutation, no flush.

    ``limit`` caps the scan so a backlog (e.g. first run after enabling
    the cron on a long-running deployment) doesn't pull thousands of
    rows into memory. The watchdog is hourly — anything past the cap
    will be picked up on the next run.
    """
    cutoff = (now or datetime.now(tz=timezone.utc)) - timedelta(hours=grace_hours)
    rows = await session.scalars(
        select(Event)
        .where(
            Event.status_type.in_(_STUCK_STATUSES),
            Event.start_time.is_not(None),
            Event.start_time < cutoff,
        )
        .order_by(Event.start_time.asc())
        .limit(limit)
    )
    return list(rows)


async def auto_void_stale_events(
    session: AsyncSession,
    *,
    void_hours: int,
    now: datetime | None = None,
    limit: int = 100,
) -> tuple[list[str], int, int]:
    """Presumptive-VOID candidates past ``void_hours``. Returns
    ``(voided_event_ids, selections_voided, deliveries_enqueued)``.

    Per event: flips ``status_type=canceled`` + ``feed_locked=True``,
    voids PENDING selections via the existing ``void_remaining_pending``
    path (which only touches PENDING rows whose source is empty/COMPUTED
    — never overrides PROVIDER/MANUAL), and enqueues ``event.voided``
    for every matching webhook endpoint.

    The caller commits. ``limit`` caps per-run blast radius — auto-void
    is destructive enough that we'd rather drip-feed than do thousands
    in one cron tick.
    """
    if void_hours <= 0:
        return [], 0, 0

    candidates = await scan_stale_events(
        session, grace_hours=void_hours, now=now, limit=limit,
    )
    if not candidates:
        return [], 0, 0

    voided_ids: list[str] = []
    total_selections_voided = 0
    total_deliveries = 0

    for event in candidates:
        prev_status = event.status_type
        event.status_type = "canceled"
        event.feed_locked = True
        event.is_finalized = False
        # Inline the VOID update — void_remaining_pending guards on
        # status_type=='finished' and we don't want to widen that
        # contract for the watchdog's canceled path.
        result = await session.execute(
            update(Selection)
            .where(
                Selection.settlement_status == "PENDING",
                Selection.market_id.in_(
                    select(Market.id).where(Market.event_id == event.id)
                ),
            )
            .values(
                settlement_status="VOID",
                settled_at=datetime.now(tz=timezone.utc),
                settlement_source="COMPUTED",
            )
        )
        sel_voided = result.rowcount or 0
        total_selections_voided += sel_voided

        await session.flush()

        deliveries = await enqueue_for_event(session, event, Transition.VOIDED)
        total_deliveries += len(deliveries)

        voided_ids.append(event.id)
        logger.warning(
            "watchdog auto-voided event=%s (was %s, start_time=%s) — "
            "%d PENDING selections → VOID, %d webhook(s) enqueued",
            event.id, prev_status, event.start_time,
            sel_voided, len(deliveries),
        )

    return voided_ids, total_selections_voided, total_deliveries


async def run_watchdog(
    session: AsyncSession,
    *,
    stale_grace_hours: int,
    auto_void_hours: int,
    now: datetime | None = None,
) -> WatchdogReport:
    """Top-level: scan stale events, then auto-void the older subset if
    enabled. Single transaction — caller commits.
    """
    stale = await scan_stale_events(
        session, grace_hours=stale_grace_hours, now=now,
    )
    stale_ids = [e.id for e in stale]

    voided_ids: list[str] = []
    selections_voided = 0
    deliveries = 0
    if auto_void_hours > 0:
        voided_ids, selections_voided, deliveries = await auto_void_stale_events(
            session, void_hours=auto_void_hours, now=now,
        )

    if stale_ids or voided_ids:
        logger.info(
            "watchdog: %d stale event(s), %d auto-voided, %d selections voided, "
            "%d webhook(s) enqueued",
            len(stale_ids), len(voided_ids), selections_voided, deliveries,
        )

    return WatchdogReport(
        stale_count=len(stale_ids),
        stale_event_ids=stale_ids,
        voided_count=len(voided_ids),
        voided_event_ids=voided_ids,
        selections_voided=selections_voided,
        deliveries_enqueued=deliveries,
    )
