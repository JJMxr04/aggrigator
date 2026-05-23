"""Unit tests for ``aggrigator.ingest.thesportsdb_translate``.

Pure-function translator: dict payload → EventSpec. No DB, no async,
no I/O. Fixture payloads are real ``eventsround.php`` rows captured
from EPL 2024-2025 round 1 / round 38.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aggrigator.ingest.thesportsdb_translate import (
    event_spec_from_thesportsdb_payload,
)


FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "thesportsdb"


def _load_event(round_num: int, idx: int = 0) -> dict:
    path = FIXTURE_DIR / f"events_round__4328__2024-2025__r{round_num}.json"
    return json.loads(path.read_text())["events"][idx]


class _StubTeam:
    """Just enough of ``aggrigator.models.Team`` to satisfy the
    translator without spinning up SQLAlchemy."""
    def __init__(self, team_id: str, league_id: str, canonical_name: str):
        self.team_id = team_id
        self.league_id = league_id
        self.canonical_name = canonical_name
        self.name_long = canonical_name
        self.name_medium = canonical_name
        self.name_short = canonical_name
        self.primary_color = None
        self.secondary_color = None
        self.primary_contrast = None
        self.secondary_contrast = None
        self.stat_entity_id = ""


def test_finished_match_full_fields():
    """The opening EPL fixture: Manchester United 1-0 Fulham 2024-08-16."""
    payload = _load_event(round_num=1, idx=0)
    home = _StubTeam("35", "EPL", "Manchester United")
    away = _StubTeam("43", "EPL", "Fulham")

    spec = event_spec_from_thesportsdb_payload(
        payload, league_id="EPL", sport_id="SOCCER",
        home_team=home, away_team=away,
    )

    assert spec is not None
    assert spec.event_id == "tsdb:2069556"  # idEvent from the payload
    assert spec.sport_id == "SOCCER"
    assert spec.league_id == "EPL"
    assert spec.type == "match"
    assert spec.season_label == "2024-2025"
    assert spec.home_score == 1
    assert spec.away_score == 0
    assert spec.winner_code == 1   # home win
    assert spec.status_type == "finished"
    assert spec.is_finalized is True
    assert spec.completed is True
    assert spec.is_live is False
    assert spec.markets == []      # no markets for thesportsdb
    # start_time is naive-from-iso, coerced to UTC by the translator
    assert spec.start_time.year == 2024 and spec.start_time.month == 8


def test_winner_codes_cover_all_outcomes():
    """Use stub payloads so we cover home / away / draw without
    hunting for matching real fixtures."""
    home = _StubTeam("A", "EPL", "A")
    away = _StubTeam("B", "EPL", "B")

    base = {
        "idEvent": "test",
        "strTimestamp": "2024-01-01T00:00:00",
        "idHomeTeam": "A", "strHomeTeam": "A",
        "idAwayTeam": "B", "strAwayTeam": "B",
        "strStatus": "Match Finished",
        "strPostponed": "no",
    }

    home_win = event_spec_from_thesportsdb_payload(
        {**base, "intHomeScore": "2", "intAwayScore": "1"},
        league_id="EPL", sport_id="SOCCER", home_team=home, away_team=away,
    )
    away_win = event_spec_from_thesportsdb_payload(
        {**base, "intHomeScore": "0", "intAwayScore": "3"},
        league_id="EPL", sport_id="SOCCER", home_team=home, away_team=away,
    )
    draw = event_spec_from_thesportsdb_payload(
        {**base, "intHomeScore": "1", "intAwayScore": "1"},
        league_id="EPL", sport_id="SOCCER", home_team=home, away_team=away,
    )

    assert home_win.winner_code == 1
    assert away_win.winner_code == 2
    assert draw.winner_code == 3


def test_missing_scores_marks_non_terminal():
    """Postponed / TBD matches arrive with null/empty scores. The
    translator must NOT mark them finalized, and winner_code stays None."""
    home = _StubTeam("A", "EPL", "A")
    away = _StubTeam("B", "EPL", "B")
    payload = {
        "idEvent": "test",
        "strTimestamp": "2024-01-01T00:00:00",
        "idHomeTeam": "A", "strHomeTeam": "A",
        "idAwayTeam": "B", "strAwayTeam": "B",
        "intHomeScore": None,
        "intAwayScore": None,
        "strStatus": "",
        "strPostponed": "no",
    }
    spec = event_spec_from_thesportsdb_payload(
        payload, league_id="EPL", sport_id="SOCCER",
        home_team=home, away_team=away,
    )
    assert spec.winner_code is None
    assert spec.is_finalized is False
    assert spec.completed is False
    assert spec.status_type == "unknown"


def test_postponed_flag():
    home = _StubTeam("A", "EPL", "A")
    away = _StubTeam("B", "EPL", "B")
    payload = {
        "idEvent": "test",
        "strTimestamp": "2024-01-01T00:00:00",
        "idHomeTeam": "A", "strHomeTeam": "A",
        "idAwayTeam": "B", "strAwayTeam": "B",
        "intHomeScore": None, "intAwayScore": None,
        "strStatus": "Postponed",
        "strPostponed": "yes",
    }
    spec = event_spec_from_thesportsdb_payload(
        payload, league_id="EPL", sport_id="SOCCER",
        home_team=home, away_team=away,
    )
    assert spec.status_type == "postponed"
    assert spec.is_finalized is False


def test_future_dated_payload_returns_none():
    """TheSportsDB is the historical source — future-dated payloads
    duplicate odds-api.io's upcoming rows. Translator must drop them."""
    home = _StubTeam("A", "EPL", "A")
    away = _StubTeam("B", "EPL", "B")
    payload = {
        "idEvent": "future-1",
        "strTimestamp": "2099-08-16T19:00:00",
        "idHomeTeam": "A", "strHomeTeam": "A",
        "idAwayTeam": "B", "strAwayTeam": "B",
        "intHomeScore": None, "intAwayScore": None,
        "strStatus": "", "strPostponed": "no",
    }
    spec = event_spec_from_thesportsdb_payload(
        payload, league_id="EPL", sport_id="SOCCER",
        home_team=home, away_team=away,
    )
    assert spec is None


def test_missing_id_returns_none():
    home = _StubTeam("A", "EPL", "A")
    away = _StubTeam("B", "EPL", "B")
    spec = event_spec_from_thesportsdb_payload(
        {"strTimestamp": "2024-01-01T00:00:00"},
        league_id="EPL", sport_id="SOCCER",
        home_team=home, away_team=away,
    )
    assert spec is None


def test_event_id_namespace_prefix():
    """``tsdb:`` prefix protects against collision with odds-api.io's
    numeric event_id namespace."""
    payload = _load_event(round_num=38, idx=0)
    home = _StubTeam("X", "EPL", "X")
    away = _StubTeam("Y", "EPL", "Y")
    spec = event_spec_from_thesportsdb_payload(
        payload, league_id="EPL", sport_id="SOCCER",
        home_team=home, away_team=away,
    )
    assert spec.event_id.startswith("tsdb:")
    assert spec.event_id != "tsdb:"  # the prefix isn't the whole id
