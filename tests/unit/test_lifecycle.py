"""Unit tests for ingest.lifecycle.decide_transition — pure function."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aggrigator.ingest.lifecycle import (
    EventState,
    Transition,
    compute_stale,
    decide_transition,
)


def _state(status: str, home: int | None = None, away: int | None = None) -> EventState:
    return EventState(status_type=status, home_score=home, away_score=away)


def test_first_seen_notstarted_is_lifecycle_changed() -> None:
    assert decide_transition(None, _state("notstarted")) == Transition.LIFECYCLE_CHANGED


def test_first_seen_inprogress_is_lifecycle_changed() -> None:
    assert decide_transition(None, _state("inprogress")) == Transition.LIFECYCLE_CHANGED


def test_first_seen_finished_fires_finalized() -> None:
    assert decide_transition(
        None, _state("finished", 27, 24)
    ) == Transition.FINALIZED


def test_notstarted_to_inprogress_is_lifecycle_changed() -> None:
    assert decide_transition(
        _state("notstarted"), _state("inprogress")
    ) == Transition.LIFECYCLE_CHANGED


def test_inprogress_to_finished_fires_finalized() -> None:
    assert decide_transition(
        _state("inprogress", 14, 10), _state("finished", 27, 24)
    ) == Transition.FINALIZED


def test_finished_unchanged_returns_none() -> None:
    assert decide_transition(
        _state("finished", 27, 24), _state("finished", 27, 24)
    ) == Transition.NONE


def test_finished_with_score_correction_re_fires_finalized() -> None:
    assert decide_transition(
        _state("finished", 27, 24), _state("finished", 28, 24)
    ) == Transition.FINALIZED


def test_first_postpone_fires_voided() -> None:
    assert decide_transition(
        _state("notstarted"), _state("postponed")
    ) == Transition.VOIDED


def test_first_cancel_fires_voided() -> None:
    assert decide_transition(
        _state("inprogress"), _state("canceled")
    ) == Transition.VOIDED


def test_postponed_to_postponed_returns_none() -> None:
    assert decide_transition(
        _state("postponed"), _state("postponed")
    ) == Transition.NONE


def test_postponed_to_canceled_fires_voided() -> None:
    """Different void status — still a meaningful state change."""
    assert decide_transition(
        _state("postponed"), _state("canceled")
    ) == Transition.VOIDED


def test_no_state_change_returns_none() -> None:
    assert decide_transition(
        _state("notstarted"), _state("notstarted")
    ) == Transition.NONE


@pytest.mark.parametrize("from_state,to_state,expected", [
    (None, "finished", Transition.FINALIZED),
    (None, "canceled", Transition.VOIDED),
    (None, "inprogress", Transition.LIFECYCLE_CHANGED),
    ("inprogress", "inprogress", Transition.NONE),
    ("notstarted", "finished", Transition.FINALIZED),
    ("postponed", "notstarted", Transition.LIFECYCLE_CHANGED),  # un-postpone
    ("canceled", "inprogress", Transition.LIFECYCLE_CHANGED),
])
def test_matrix(from_state, to_state, expected) -> None:
    prev = _state(from_state) if from_state else None
    new = _state(to_state, 1, 0)
    assert decide_transition(prev, new) == expected


# ---- compute_stale --------------------------------------------------------


_NOW = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)


def test_compute_stale_notstarted_past_grace_is_stale() -> None:
    start = _NOW - timedelta(hours=24)
    assert compute_stale(
        status_type="notstarted", start_time=start, grace_hours=12, now=_NOW,
    ) is True


def test_compute_stale_inprogress_past_grace_is_stale() -> None:
    """Stuck live games count too — a 'live' game 20 hours past start
    is just as stale as a notstarted one."""
    start = _NOW - timedelta(hours=20)
    assert compute_stale(
        status_type="inprogress", start_time=start, grace_hours=12, now=_NOW,
    ) is True


def test_compute_stale_within_grace_is_fresh() -> None:
    start = _NOW - timedelta(hours=2)
    assert compute_stale(
        status_type="notstarted", start_time=start, grace_hours=12, now=_NOW,
    ) is False


def test_compute_stale_future_event_is_fresh() -> None:
    start = _NOW + timedelta(hours=4)
    assert compute_stale(
        status_type="notstarted", start_time=start, grace_hours=12, now=_NOW,
    ) is False


def test_compute_stale_finished_never_stale() -> None:
    """Terminal statuses are settled; staleness doesn't apply."""
    start = _NOW - timedelta(days=10)
    assert compute_stale(
        status_type="finished", start_time=start, grace_hours=12, now=_NOW,
    ) is False


def test_compute_stale_postponed_never_stale() -> None:
    start = _NOW - timedelta(days=10)
    assert compute_stale(
        status_type="postponed", start_time=start, grace_hours=12, now=_NOW,
    ) is False


def test_compute_stale_missing_start_time_is_fresh() -> None:
    assert compute_stale(
        status_type="notstarted", start_time=None, grace_hours=12, now=_NOW,
    ) is False


def test_compute_stale_unknown_status_is_fresh() -> None:
    """Unknown provider status — better to miss-flag than wrongly act."""
    start = _NOW - timedelta(days=2)
    assert compute_stale(
        status_type="weather_delay", start_time=start, grace_hours=12, now=_NOW,
    ) is False


def test_compute_stale_case_insensitive() -> None:
    start = _NOW - timedelta(hours=24)
    assert compute_stale(
        status_type="NOTSTARTED", start_time=start, grace_hours=12, now=_NOW,
    ) is True
