"""Integration test for ``ingest_historical_league`` (TheSportsDB).

Uses a fixture client that reads pre-captured ``eventsround.php`` JSON
from ``tests/fixtures/thesportsdb/`` so the test never touches the real
TheSportsDB API.

Verifies:
1. Gate behavior — refuses leagues missing ``can_pull_historical_scores``.
2. End-to-end upsert — round 1 + round 38 → 20 Event rows
   (10 per round) with ``provider='thesportsdb'``, scores populated,
   zero Market rows.
3. Idempotency — re-running on the same fixture set produces the same
   row count and the same event_ids.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from aggrigator.ingest.historical_orchestrator import (
    HistoricalIngestError,
    ingest_historical_league,
)
from aggrigator.ingest.thesportsdb_client import TheSportsDbClient
from aggrigator.models import Event, League, Market, Sport, Team


FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "thesportsdb"


class FixtureTheSportsDbClient(TheSportsDbClient):
    """Reads ``eventsround.php`` payloads from on-disk fixture JSON
    instead of hitting the real API. The orchestrator only calls
    ``events_round`` — other methods raise so a stray call is loud."""

    def __init__(self, fixture_dir: Path = FIXTURE_DIR):
        # Do NOT call super().__init__ — we don't need httpx or a real
        # session for a fixture client.
        self.fixture_dir = fixture_dir

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def aclose(self):
        pass

    async def events_round(self, league_id: str, round_num: int, season: str):
        path = self.fixture_dir / (
            f"events_round__{league_id}__{season}__r{round_num}.json"
        )
        if not path.exists():
            return []
        return json.loads(path.read_text()).get("events") or []


async def _seed_epl(session):
    """Insert just enough of the SOCCER / EPL / Team rows for the
    orchestrator's team_lookup to resolve the fixture's idHomeTeam /
    idAwayTeam values. Mirrors the registry-loader's rows."""
    sport = Sport(id="SOCCER", name="Soccer", active=True)
    session.add(sport)
    await session.flush()

    league = League(
        id="EPL", sport_id="SOCCER",
        name="English Premier League",
        thesportsdb_id="4328",
        can_pull_historical_scores=True,
        active=True,
    )
    session.add(league)
    await session.flush()

    # Pull the (idTeam, strTeam) set out of the fixture rounds so the
    # team table covers everyone that plays in r1+r38.
    teams: dict[str, str] = {}
    for fixture_file in FIXTURE_DIR.glob("events_round__4328__*__r*.json"):
        data = json.loads(fixture_file.read_text())
        for ev in data.get("events") or []:
            for id_k, name_k in (("idHomeTeam", "strHomeTeam"),
                                 ("idAwayTeam", "strAwayTeam")):
                tid = ev.get(id_k)
                tname = ev.get(name_k)
                if tid and tname and tid not in teams:
                    teams[tid] = tname

    for tsdb_id, name in teams.items():
        session.add(Team(
            id=Team.synth_pk("EPL", tsdb_id),
            league_id="EPL",
            team_id=tsdb_id,
            sport_id="SOCCER",
            name_long=name,
            canonical_name=name,
            thesportsdb_team_id=tsdb_id,
            match_confirmed=True,
            match_source="fuzzy_match",
        ))
    await session.commit()


async def test_gate_refuses_ineligible_league(session):
    """League with can_pull_historical_scores=False -> immediate raise,
    no fixture client calls."""
    session.add(Sport(id="SOCCER", name="Soccer", active=True))
    await session.flush()
    session.add(League(
        id="EPL", sport_id="SOCCER", name="EPL",
        thesportsdb_id="4328",
        can_pull_historical_scores=False,
    ))
    await session.commit()

    with pytest.raises(HistoricalIngestError, match="not eligible"):
        await ingest_historical_league(
            session, "EPL", "2024-2025",
            client=FixtureTheSportsDbClient(),
            rounds=[1],
        )


async def test_round_walk_writes_events_no_markets(session):
    """End-to-end: round 1 + round 38 → 20 EPL events with scores,
    zero markets."""
    await _seed_epl(session)
    report = await ingest_historical_league(
        session, "EPL", "2024-2025",
        client=FixtureTheSportsDbClient(),
        rounds=[1, 38],
    )

    assert report.rounds_fetched == 2
    assert report.events_upserted == 20
    assert report.events_skipped == 0

    events = (await session.scalars(
        select(Event).where(Event.league_id == "EPL")
    )).all()
    assert len(events) == 20
    assert all(e.provider == "thesportsdb" for e in events)
    assert all(e.event_id.startswith("tsdb:") if hasattr(e, "event_id") else e.id.startswith("tsdb:")
               for e in events)
    assert all(e.home_score is not None and e.away_score is not None for e in events)
    assert all(e.winner_code in (1, 2, 3) for e in events)

    markets = (await session.scalars(
        select(Market).where(Market.event_id.in_([e.id for e in events]))
    )).all()
    assert len(markets) == 0


async def test_idempotent_rerun(session):
    """Re-running the same backfill yields the same row count + ids."""
    await _seed_epl(session)
    await ingest_historical_league(
        session, "EPL", "2024-2025",
        client=FixtureTheSportsDbClient(),
        rounds=[1, 38],
    )
    first_ids = sorted([
        e.id for e in (await session.scalars(
            select(Event).where(Event.league_id == "EPL")
        )).all()
    ])

    await ingest_historical_league(
        session, "EPL", "2024-2025",
        client=FixtureTheSportsDbClient(),
        rounds=[1, 38],
    )
    second_ids = sorted([
        e.id for e in (await session.scalars(
            select(Event).where(Event.league_id == "EPL")
        )).all()
    ])

    assert first_ids == second_ids
    assert len(second_ids) == 20
