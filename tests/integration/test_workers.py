"""Tests for the ARQ task functions.

We don't actually run ARQ here (that would need a Redis fixture); the task
functions are plain async functions, so we call them directly to prove they
do the right side-effects against a real DB. The ARQ scheduling mechanism is
trusted to call them on its cron.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from aggrigator.config import get_settings
from aggrigator.models import Sport, WebhookDelivery
from tests.fixtures.oddsapi_client import FixtureOddsClient
from aggrigator.workers.tasks.webhook_deliver import run_deliver_due
from tests.integration.factories import (
    make_event,
    make_league,
    make_market,
    make_selection,
    make_sport,
    make_team,
)

pytestmark = pytest.mark.asyncio


TEST_SECRET = "worker-test-secret"
TEST_URL = "https://example.test/hook"


@pytest.fixture(autouse=True)
def _set_webhook_target():
    s = get_settings()
    original_url = s.webhook_target_url
    original_secret = s.webhook_secret
    s.webhook_target_url = TEST_URL
    s.webhook_secret = TEST_SECRET
    yield
    s.webhook_target_url = original_url
    s.webhook_secret = original_secret


# ---- seeder ---------------------------------------------------------------


async def test_seed_sports_and_leagues_against_fixture(
    session, oddsapi_fixture_dir: Path,
) -> None:
    from aggrigator.ingest.seed import seed_all

    client = FixtureOddsClient(oddsapi_fixture_dir)
    summary = await seed_all(session, client)
    await session.commit()

    assert summary["sports"] > 0
    assert summary["leagues"] > 0

    # Sanity: the canonical NFL pair is present.
    sport = await session.get(Sport, "FOOTBALL")
    assert sport is not None
    leagues = list(await session.scalars(select(__import__("aggrigator").models.League)))
    assert any(lg.id == "NFL" for lg in leagues)


# ---- webhook_deliver worker -----------------------------------------------


async def _seed_event_with_market(session, *, status_type: str = "finished"):
    sport = await make_sport(session, id="FOOTBALL", name="Football")
    league = await make_league(session, sport=sport, id="NFL", name="NFL", active=True)
    home = await make_team(session, league=league, team_id="DAL")
    away = await make_team(session, league=league, team_id="PHI", name_long="Philly")
    event = await make_event(
        session, league=league, home_team=home, away_team=away,
        id="evt-deliver-test", status_type=status_type,
        is_finalized=(status_type == "finished"),
        completed=(status_type == "finished"),
        home_score=27, away_score=24, winner_code=1,
    )
    market = await make_market(session, event=event, type="NFL_POINTS_ML")
    await make_selection(session, market=market, type="HOME")
    return event


def _patch_httpx(monkeypatch, transport: httpx.MockTransport) -> None:
    class _PatchedClient(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    monkeypatch.setattr(
        "aggrigator.workers.tasks.webhook_deliver.httpx.AsyncClient",
        _PatchedClient,
    )


async def test_run_deliver_due_drains_pending(session, monkeypatch) -> None:
    """Plant a delivery row, point it at a mock-handler URL, run the worker."""
    event = await _seed_event_with_market(session)

    delivery = WebhookDelivery(
        event_id=event.id,
        event_name="event.finalized",
        idempotency_key=f"{event.id}:abcd",
        payload={"schema_version": 1, "event": {"event_id": event.id}},
    )
    session.add(delivery)
    await session.commit()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    _patch_httpx(monkeypatch, httpx.MockTransport(handler))

    summary = await run_deliver_due()
    assert summary["sent"] == 1

    refreshed = await session.get(WebhookDelivery, delivery.id)
    assert refreshed is not None
    await session.refresh(refreshed)
    assert refreshed.delivered_at is not None


async def test_run_deliver_due_skips_when_target_unset(session, monkeypatch) -> None:
    """No-op when the operator hasn't configured AGG_WEBHOOK_TARGET_URL."""
    s = get_settings()
    monkeypatch.setattr(s, "webhook_target_url", "")
    event = await _seed_event_with_market(session)
    delivery = WebhookDelivery(
        event_id=event.id,
        event_name="event.finalized",
        idempotency_key=f"{event.id}:unset",
        payload={"schema_version": 1, "event": {"event_id": event.id}},
    )
    session.add(delivery)
    await session.commit()

    summary = await run_deliver_due()
    assert summary == {"sent": 0, "retried": 0, "failed": 0}
    refreshed = await session.get(WebhookDelivery, delivery.id)
    await session.refresh(refreshed)
    assert refreshed.delivered_at is None


async def test_run_deliver_due_retries_on_5xx(session, monkeypatch) -> None:
    event = await _seed_event_with_market(session)

    delivery = WebhookDelivery(
        event_id=event.id,
        event_name="event.finalized",
        idempotency_key=f"{event.id}:retry-test",
        payload={"schema_version": 1, "event": {"event_id": event.id}},
    )
    session.add(delivery)
    await session.commit()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    _patch_httpx(monkeypatch, httpx.MockTransport(handler))

    summary = await run_deliver_due()
    assert summary == {"sent": 0, "retried": 1, "failed": 0}
    refreshed = await session.get(WebhookDelivery, delivery.id)
    await session.refresh(refreshed)
    assert refreshed.delivered_at is None
    assert refreshed.next_retry_at is not None
