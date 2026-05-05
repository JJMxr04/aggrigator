"""Event-lifecycle transition predicates.

Pure functions, no DB. Mirror MDProject's ``_should_settle`` / ``_should_reopen``
in ``core/event/models/event.py`` plus the plan §6 state machine. The
orchestrator calls ``decide_transition`` after upserting an event row to figure
out which webhook (if any) to enqueue.

Transitions:
- ``FINALIZED``        — first time event reaches ``finished``, OR re-fires
                          on score correction (when ``home_score``/``away_score``
                          change while still ``finished``).
- ``VOIDED``           — first time event becomes ``postponed`` or ``canceled``.
- ``LIFECYCLE_CHANGED``— any other ``status_type`` transition that isn't covered
                          above (e.g., ``notstarted -> inprogress``).
- ``NONE``             — nothing notable changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum


class Transition(StrEnum):
    NONE = "none"
    LIFECYCLE_CHANGED = "event.lifecycle.changed"
    FINALIZED = "event.finalized"
    VOIDED = "event.voided"


# Statuses that mean the event is over and won't change again. Used by
# the ingest cron to skip backfilling events SGO has already finished
# but our DB has never seen — pointless DB rows + SGO entity burn — and
# by the vacuum cron to identify rows safe to delete after retention.
TERMINAL_STATUSES: tuple[str, ...] = ("finished", "postponed", "canceled")


@dataclass(frozen=True)
class EventState:
    """Just enough of an Event to make a transition decision."""
    status_type: str
    home_score: int | None
    away_score: int | None


def decide_transition(
    previous: EventState | None, new: EventState
) -> Transition:
    """Decide which webhook (if any) the upsert should fire.

    Mirrors `core/event/models/event.py:_should_settle` / `_should_reopen`
    plus a third "lifecycle changed" bucket for non-finished status flips.
    """
    if new.status_type == "finished":
        # First time finished, OR score corrected on an already-finished event.
        if previous is None or previous.status_type != "finished":
            return Transition.FINALIZED
        if (previous.home_score, previous.away_score) != (new.home_score, new.away_score):
            return Transition.FINALIZED
        return Transition.NONE

    if new.status_type in ("postponed", "canceled"):
        if previous is None or previous.status_type != new.status_type:
            return Transition.VOIDED
        return Transition.NONE

    # All other status_type values: notstarted, inprogress, …
    if previous is None or previous.status_type != new.status_type:
        return Transition.LIFECYCLE_CHANGED
    return Transition.NONE


# Statuses that mean "should be over by now if start_time is in the past"
# — used by the watchdog to flag/auto-void events SGO never finalized.
# Excludes the terminal set (those are already settled one way or another)
# and excludes any future status SGO might invent (we'd rather miss-flag
# than wrongly void a status we don't know about).
_NON_TERMINAL_STATUSES: tuple[str, ...] = ("notstarted", "inprogress")


def compute_stale(
    *,
    status_type: str | None,
    start_time: datetime | None,
    grace_hours: int,
    now: datetime | None = None,
) -> bool:
    """Pure predicate: is this event "stuck"?

    True when the event's ``start_time`` is more than ``grace_hours`` in the
    past AND its ``status_type`` is still ``notstarted`` / ``inprogress``.
    Most often a postponed game SGO never re-statused.

    Returns False for terminal statuses (already done), missing start_time,
    future events, and any unknown status_type. ``now`` is injectable for
    deterministic tests.
    """
    if not start_time or not status_type:
        return False
    if status_type.lower() not in _NON_TERMINAL_STATUSES:
        return False
    cutoff_now = now or datetime.now(tz=timezone.utc)
    return start_time < cutoff_now - timedelta(hours=grace_hours)
