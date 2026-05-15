"""Integration tests for ingest.watchdog — stale-event detection + auto-void.

Verifies the end-to-end recovery path for events the provider never finalized:
1. Stale events are surfaced (no DB writes when auto-void disabled).
2. With auto-void on, events past the threshold are flipped to canceled,
   PENDING selections become VOID, and event.voided webhook deliveries
   land in the queue.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aggrigator.config import get_settings
from aggrigator.ingest.watchdog import run_watchdog
from aggrigator.models import Event, Selection, WebhookDelivery
from tests.integration.factories import (
    make_event,
    make_league,
    make_market,
    make_selection,
    make_sport,
    make_team,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _set_webhook_target():
    """Configure the dispatcher so the auto-void path enqueues a delivery."""
    s = get_settings()
    original_url = s.webhook_target_url
    original_secret = s.webhook_secret
    s.webhook_target_url = "https://example.test/voided-hook"
    s.webhook_secret = "watchdog-test-secret"
    yield
    s.webhook_target_url = original_url
    s.webhook_secret = original_secret


async def _seed_stale_event(session, *, hours_past_start: int, status: str = "notstarted"):
    """Plant one event whose start_time is N hours in the past, with
    one PENDING selection. Returns the event."""
    sport = await make_sport(session, id="BASEBALL", name="Baseball")
    league = await make_league(session, sport=sport, id="MLB", name="MLB")
    home = await make_team(session, league=league, team_id="LAD")
    away = await make_team(session, league=league, team_id="SFG", name_long="Giants")
    event = await make_event(
        session, league=league, home_team=home, away_team=away,
        id=f"evt-stale-{hours_past_start}",
        start_time=datetime.now(tz=timezone.utc) - timedelta(hours=hours_past_start),
        status_type=status,
    )
    market = await make_market(session, event=event, type="MLB_RUNS_ML")
    await make_selection(session, market=market, type="HOME")
    return event


async def test_watchdog_flags_stale_without_auto_void(session) -> None:
    """auto_void_hours=0 → scan only, no mutation."""
    event = await _seed_stale_event(session, hours_past_start=18)

    report = await run_watchdog(
        session, stale_grace_hours=12, auto_void_hours=0,
    )
    await session.commit()

    assert report.stale_count == 1
    assert event.id in report.stale_event_ids
    assert report.voided_count == 0
    assert report.selections_voided == 0

    # Row untouched.
    refreshed = await session.get(Event, event.id)
    assert refreshed is not None
    assert refreshed.status_type == "notstarted"
    assert refreshed.feed_locked is False


async def test_watchdog_skips_recent_events(session) -> None:
    """Event within grace window is not stale."""
    await _seed_stale_event(session, hours_past_start=4)

    report = await run_watchdog(
        session, stale_grace_hours=12, auto_void_hours=0,
    )
    assert report.stale_count == 0


async def test_watchdog_auto_voids_past_threshold(session) -> None:
    """auto_void_hours triggers status flip + selection void + webhook enqueue."""
    event = await _seed_stale_event(session, hours_past_start=72)

    report = await run_watchdog(
        session, stale_grace_hours=12, auto_void_hours=48,
    )
    await session.commit()

    assert report.stale_count == 1
    assert report.voided_count == 1
    assert event.id in report.voided_event_ids
    assert report.selections_voided == 1
    assert report.deliveries_enqueued == 1

    # Event flipped.
    refreshed = await session.get(Event, event.id)
    assert refreshed is not None
    assert refreshed.status_type == "canceled"
    assert refreshed.feed_locked is True
    assert refreshed.is_finalized is False

    # Selection voided with COMPUTED source.
    from sqlalchemy import select
    sels = list(await session.scalars(select(Selection)))
    assert len(sels) == 1
    assert sels[0].settlement_status == "VOID"
    assert sels[0].settlement_source == "COMPUTED"
    assert sels[0].settled_at is not None

    # Webhook delivery enqueued.
    deliveries = list(await session.scalars(select(WebhookDelivery)))
    assert len(deliveries) == 1
    assert deliveries[0].event_name == "event.voided"
    assert deliveries[0].event_id == event.id


async def test_watchdog_auto_void_respects_status_filter(session) -> None:
    """Already-finished events are ignored even if start_time is ancient."""
    sport = await make_sport(session, id="BASEBALL", name="Baseball")
    league = await make_league(session, sport=sport, id="MLB", name="MLB")
    home = await make_team(session, league=league, team_id="NYY")
    away = await make_team(session, league=league, team_id="BOS", name_long="Sox")
    finished = await make_event(
        session, league=league, home_team=home, away_team=away,
        id="evt-finished-old",
        start_time=datetime.now(tz=timezone.utc) - timedelta(hours=72),
        status_type="finished",
        is_finalized=True,
        completed=True,
        home_score=4, away_score=2, winner_code=1,
    )

    report = await run_watchdog(
        session, stale_grace_hours=12, auto_void_hours=48,
    )

    assert report.stale_count == 0
    assert report.voided_count == 0

    refreshed = await session.get(Event, finished.id)
    assert refreshed.status_type == "finished"  # untouched
