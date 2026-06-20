"""Sport.active gates league seeding.

Guard/characterization test: an active sport seeds all its payload leagues
(landing active=False); an inactive sport seeds none. The gating already
exists in seed_leagues — this test proves it holds and will catch any
regression that accidentally bypasses it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from aggrigator.ingest.seed import seed_leagues
from aggrigator.models import League
from tests.integration.factories import make_sport

pytestmark = pytest.mark.asyncio


async def test_active_sport_seeds_all_catalog_leagues_inactive(session) -> None:
    # make_sport commits; two sports with opposing active flags.
    await make_sport(session, id="SOCCER", name="Soccer", active=True)
    await make_sport(session, id="BASEBALL", name="Baseball", active=False)

    payloads = [
        {"leagueID": "EPL", "sportID": "SOCCER", "name": "EPL", "shortName": "EPL"},
        {
            "leagueID": "ARGENTINA_PRIMERA_B",
            "sportID": "SOCCER",
            "name": "Argentina - Primera B",
            "shortName": "ARG B",
        },
        # BASEBALL is inactive — this league must be skipped.
        {"leagueID": "MLB", "sportID": "BASEBALL", "name": "MLB", "shortName": "MLB"},
    ]
    count = await seed_leagues(session, None, _payloads=payloads)
    await session.flush()

    soccer_rows = (
        await session.scalars(select(League).where(League.sport_id == "SOCCER"))
    ).all()
    assert {r.id for r in soccer_rows} == {"EPL", "ARGENTINA_PRIMERA_B"}
    # New leagues must land inactive; operator enables them deliberately.
    assert all(r.active is False for r in soccer_rows)

    # Inactive parent sport → its league is not seeded.
    mlb_rows = (
        await session.scalars(select(League).where(League.id == "MLB"))
    ).all()
    assert mlb_rows == []

    assert count == 2
