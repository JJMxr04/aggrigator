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
