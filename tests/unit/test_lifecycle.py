"""Unit tests for ingest.lifecycle.decide_transition — pure function."""

from __future__ import annotations

import pytest

from aggrigator.ingest.lifecycle import EventState, Transition, decide_transition


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
