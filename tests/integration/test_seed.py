"""Seed against real Postgres: phase-collapsed leagueIDs must not crash.

Companion to ``tests/unit/test_seed_dedupe.py`` — the unit test pins the pure
de-dup helper; this one proves the end-to-end invariant against the real
``pk_core_league`` unique constraint with the production ``autoflush=False``
session, which is where the original ``UniqueViolationError`` surfaced.

Skips unless ``AGG_TEST_DATABASE_URL`` is set (see integration/conftest.py).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from aggrigator.ingest.seed import seed_leagues, seed_sports
from aggrigator.models import League, Sport
from tests.integration.factories import make_sport

pytestmark = pytest.mark.asyncio


async def test_seed_leagues_collapses_phase_duplicate_league_ids(session) -> None:
    await make_sport(session, id="BASKETBALL", name="Basketball", active=True)
    await session.flush()

    # Same internal leagueID twice — the usa-nba / usa-nba-playoffs collapse.
    payloads = [
        {"leagueID": "NBA", "sportID": "BASKETBALL", "name": "NBA", "shortName": "NBA"},
        {"leagueID": "NBA", "sportID": "BASKETBALL", "name": "NBA Playoffs", "shortName": "NBA PO"},
    ]
    count = await seed_leagues(session, None, _payloads=payloads)
    await session.flush()

    rows = (await session.scalars(select(League).where(League.id == "NBA"))).all()
    assert len(rows) == 1
    assert rows[0].name == "NBA"  # first occurrence wins
    assert count == 1


async def test_seed_sports_collapses_duplicate_sport_ids(session) -> None:
    payloads = [
        {"sportID": "BASKETBALL", "name": "Basketball", "shortName": "BBALL"},
        {"sportID": "BASKETBALL", "name": "Basketball", "shortName": "BBALL"},
    ]
    count = await seed_sports(session, None, _payloads=payloads)
    await session.flush()

    rows = (await session.scalars(select(Sport).where(Sport.id == "BASKETBALL"))).all()
    assert len(rows) == 1
    assert count == 1
