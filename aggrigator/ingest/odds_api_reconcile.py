"""Disappearance reconciliation — handle events that vanish from the
upstream feed.

odds-api.io has no explicit ``cancelled`` / ``postponed`` status (verified
against the OpenAPI spec). The only REST signal for a cancellation is
**absence**: the event stops appearing in ``GET /events?sport=...``.

This module compares "events the DB has in-window" against "events the
adapter returned this cycle" and flags the missing ones by setting
``Event.last_seen_upstream_at`` to a stale timestamp. The watchdog
(``lifecycle.run_watchdog``) then auto-VOIDs anything that stays missing
past the disappearance grace.

Two-clock design:

- ``last_seen_upstream_at`` — the disappearance clock, independent of
  ``start_time``. A pre-match cancellation 6h before kickoff has to be
  catchable even though the existing stale-pending watchdog only fires
  *after* start time.
- Stale-pending watchdog (existing) — the post-start clock. Unchanged
  semantics.

The watchdog's disappearance branch flips an event to VOID only when
BOTH clocks have elapsed past their thresholds. That prevents a transient
upstream blip (one cycle of absence) from voiding active events.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.models import Event

logger = logging.getLogger(__name__)


async def reconcile_disappeared(
    session: AsyncSession,
    *,
    league_id: str,
    seen_event_ids: set[str],
    window_start: datetime,
    window_end: datetime,
) -> list[str]:
    """Mark as stale any pending event the DB has in-window that the
    adapter didn't return this cycle. Returns the list of stale ids.

    Does NOT void — that's the watchdog's job. We only roll back
    ``last_seen_upstream_at`` to "now - cycle" so the watchdog sees
    consecutive misses across multiple cycles.

    The caller flushes/commits.
    """
    # Pull pending event ids the DB thinks should be in window.
    db_pending: set[str] = set(
        await session.scalars(
            select(Event.id).where(
                Event.league_id == league_id,
                Event.start_time.is_not(None),
                Event.start_time >= window_start,
                Event.start_time <= window_end,
                Event.is_finalized.is_(False),
                Event.status_type.notin_(("canceled", "postponed")),
            )
        )
    )
    missing = sorted(db_pending - seen_event_ids)
    if not missing:
        return []

    # Roll the disappearance clock back by ~1 cycle (1 hour) for missing
    # rows so the watchdog can tell "this row hasn't been seen for N
    # cycles." Successful upserts will refresh the timestamp to NOW on
    # the next cycle the event reappears.
    stale_at = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    await session.execute(
        update(Event)
        .where(Event.id.in_(missing))
        .values(last_seen_upstream_at=stale_at)
    )
    logger.warning(
        "oddsapi reconcile: %d event(s) missing from upstream for league %s — "
        "rolled last_seen_upstream_at; watchdog will auto-void after grace. "
        "First 10: %s",
        len(missing), league_id, missing[:10],
    )
    return missing
