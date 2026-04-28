"""FixtureSgoClient — sanity checks against the captured corpus."""

from __future__ import annotations

from pathlib import Path

import pytest

from aggrigator.ingest.client import SgoError
from aggrigator.ingest.sgo_simulator import FixtureSgoClient


def test_client_rejects_missing_dir() -> None:
    with pytest.raises(SgoError):
        FixtureSgoClient("/tmp/does-not-exist-aggrigator-test")


def test_get_sports_returns_list(sgo_fixture_dir: Path) -> None:
    client = FixtureSgoClient(sgo_fixture_dir)
    sports = client.get_sports()
    assert isinstance(sports, list)
    assert len(sports) > 0
    assert all("sportID" in s for s in sports)


def test_get_leagues_for_known_sport(sgo_fixture_dir: Path) -> None:
    client = FixtureSgoClient(sgo_fixture_dir)
    leagues = client.get_leagues(sport_id="FOOTBALL")
    assert any(lg.get("leagueID") == "NFL" for lg in leagues)


def test_get_events_for_nfl(sgo_fixture_dir: Path) -> None:
    client = FixtureSgoClient(sgo_fixture_dir)
    events = list(client.get_events(league_id="NFL"))
    assert len(events) > 0
    assert all(e.get("leagueID") == "NFL" for e in events)


def test_get_event_by_id_round_trip(sgo_fixture_dir: Path) -> None:
    client = FixtureSgoClient(sgo_fixture_dir)
    nfl = list(client.get_events(league_id="NFL"))
    if not nfl:
        pytest.skip("No NFL fixture events")
    target = nfl[0]["eventID"]
    found = client.get_event(target)
    assert found is not None
    assert found["eventID"] == target


def test_get_account_usage(sgo_fixture_dir: Path) -> None:
    client = FixtureSgoClient(sgo_fixture_dir)
    usage = client.get_account_usage()
    assert isinstance(usage, dict)
