"""Integration tests for ``odds_api_reconcile.reconcile_disappeared``.

Skipped automatically when no Postgres is configured (no
``AGG_TEST_DATABASE_URL``) — see ``tests/integration/conftest.py``.

Verifies:
- events the adapter returned keep their ``last_seen_upstream_at``.
- events the DB has in-window that the adapter DIDN'T return get their
  ``last_seen_upstream_at`` rolled back.
- finalized and already-canceled rows are excluded (idempotent).
- out-of-window rows are not touched (the watchdog has its own cadence).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aggrigator.ingest.odds_api_reconcile import reconcile_disappeared
from tests.integration.factories import (
    make_event,
    make_league,
    make_sport,
    make_team,
)

pytestmark = pytest.mark.asyncio


async def _seed_event_in_window(
    session, *,
    id: str,
    start_offset_hours: int = 2,
    status_type: str = "notstarted",
    is_finalized: bool = False,
):
    sport = await make_sport(session, id="BASEBALL", name="Baseball")
    league = await make_league(session, sport=sport, id="MLB", name="MLB")
    home = await make_team(session, league=league, team_id=f"H-{id}")
    away = await make_team(
        session, league=league, team_id=f"A-{id}", name_long=f"Away-{id}",
    )
    event = await make_event(
        session, league=league, home_team=home, away_team=away,
        id=id,
        start_time=datetime.now(tz=timezone.utc) + timedelta(hours=start_offset_hours),
        status_type=status_type,
        is_finalized=is_finalized,
    )
    # Seed last_seen_upstream_at = now so the row looks freshly ingested.
    event.last_seen_upstream_at = datetime.now(tz=timezone.utc)
    await session.commit()
    return event


async def test_seen_event_untouched(session) -> None:
    e = await _seed_event_in_window(session, id="evt-seen")
    seen_before = e.last_seen_upstream_at

    now = datetime.now(tz=timezone.utc)
    missing = await reconcile_disappeared(
        session,
        league_id="MLB",
        seen_event_ids={"evt-seen"},
        window_start=now - timedelta(days=1),
        window_end=now + timedelta(days=2),
    )
    await session.commit()
    assert missing == []
    await session.refresh(e)
    assert e.last_seen_upstream_at == seen_before


async def test_missing_event_gets_last_seen_rolled_back(session) -> None:
    e = await _seed_event_in_window(session, id="evt-vanished")
    fresh = e.last_seen_upstream_at

    now = datetime.now(tz=timezone.utc)
    missing = await reconcile_disappeared(
        session,
        league_id="MLB",
        seen_event_ids=set(),  # adapter didn't return it
        window_start=now - timedelta(days=1),
        window_end=now + timedelta(days=2),
    )
    await session.commit()
    assert missing == ["evt-vanished"]
    await session.refresh(e)
    assert e.last_seen_upstream_at < fresh


async def test_finalized_event_not_flagged(session) -> None:
    e = await _seed_event_in_window(
        session, id="evt-final",
        start_offset_hours=-3, status_type="finished", is_finalized=True,
    )
    fresh = e.last_seen_upstream_at

    now = datetime.now(tz=timezone.utc)
    missing = await reconcile_disappeared(
        session,
        league_id="MLB",
        seen_event_ids=set(),
        window_start=now - timedelta(days=1),
        window_end=now + timedelta(days=2),
    )
    await session.commit()
    assert missing == []
    await session.refresh(e)
    assert e.last_seen_upstream_at == fresh


async def test_canceled_event_not_flagged(session) -> None:
    e = await _seed_event_in_window(
        session, id="evt-canceled",
        start_offset_hours=-3, status_type="canceled",
    )
    fresh = e.last_seen_upstream_at

    now = datetime.now(tz=timezone.utc)
    missing = await reconcile_disappeared(
        session,
        league_id="MLB",
        seen_event_ids=set(),
        window_start=now - timedelta(days=1),
        window_end=now + timedelta(days=2),
    )
    await session.commit()
    assert missing == []
    await session.refresh(e)
    assert e.last_seen_upstream_at == fresh


async def test_out_of_window_event_not_flagged(session) -> None:
    e = await _seed_event_in_window(
        session, id="evt-future", start_offset_hours=24 * 30,
    )
    fresh = e.last_seen_upstream_at

    now = datetime.now(tz=timezone.utc)
    missing = await reconcile_disappeared(
        session,
        league_id="MLB",
        seen_event_ids=set(),
        window_start=now - timedelta(days=1),
        window_end=now + timedelta(days=2),
    )
    await session.commit()
    assert missing == []
    await session.refresh(e)
    assert e.last_seen_upstream_at == fresh
