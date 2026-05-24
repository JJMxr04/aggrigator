"""End-to-end webhook delivery tests.

Covers:
- ingest pipeline produces a WebhookDelivery row when transition != NONE
- enqueue is idempotent on unchanged event state (no duplicate row)
- send_one applies the retry / success / permanent-fail rules
- the receiver-side signature verification works against what we sign

Single hardcoded target — the URL + HMAC secret come from settings
(``AGG_WEBHOOK_TARGET_URL`` / ``AGG_WEBHOOK_SECRET``). Tests monkeypatch
those values to seed the enqueue path.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from aggrigator.config import get_settings
from aggrigator.ingest.lifecycle import Transition
from aggrigator.ingest.orchestrator import ingest_event
from aggrigator.models import WebhookDelivery
from tests.fixtures.oddsapi_client import FixtureOddsClient
from aggrigator.security.webhook_signing import HEADER_NAME, verify
from aggrigator.webhooks.deliver import (
    MAX_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    send_one,
)
from tests.integration.factories import (
    make_event,
    make_league,
    make_market,
    make_selection,
    make_sport,
    make_team,
)

pytestmark = pytest.mark.asyncio


TEST_SECRET = "shared-test-secret-deadbeef"
TEST_BASE_URL = "https://example.com"
# Matches what Settings.webhook_target_url derives from TEST_BASE_URL +
# MDPROJECT_WEBHOOK_PATH — kept in sync so tests that call ``send_one``
# directly pass the same URL the deploy would actually use.
TEST_URL = f"{TEST_BASE_URL}/sportgameodds/webhook"


@pytest.fixture(autouse=True)
def _set_webhook_target():
    """Point the dispatcher at a placeholder URL so enqueue produces rows."""
    s = get_settings()
    original_base = s.mdproject_url
    original_secret = s.webhook_secret
    s.mdproject_url = TEST_BASE_URL
    s.webhook_secret = TEST_SECRET
    yield
    s.mdproject_url = original_base
    s.webhook_secret = original_secret


# ---- helpers ---------------------------------------------------------------


async def _seed_finished_event(session):
    sport = await make_sport(session, id="FOOTBALL", name="Football")
    league = await make_league(session, sport=sport, id="NFL", name="NFL", active=True)
    home = await make_team(session, league=league, team_id="DAL")
    away = await make_team(session, league=league, team_id="PHI", name_long="Philly")
    event = await make_event(
        session, league=league, home_team=home, away_team=away,
        id="evt-fin", status_type="finished", is_finalized=True, completed=True,
        home_score=27, away_score=24, winner_code=1,
    )
    market = await make_market(session, event=event, type="NFL_POINTS_ML")
    await make_selection(session, market=market, type="HOME")
    return event


# ---- enqueue path (via orchestrator) ---------------------------------------


async def test_ingest_enqueues_delivery_when_target_configured(
    session, oddsapi_fixture_dir,
) -> None:
    from aggrigator.models import Sport

    await make_sport(session, id="FOOTBALL", name="Football")
    sport = await session.get(Sport, "FOOTBALL")
    await make_league(session, sport=sport, id="NFL", name="NFL", active=True)

    client = FixtureOddsClient(oddsapi_fixture_dir)
    payload = next(p for p in client.get_events(league_id="NFL") if p.get("type") == "match")
    result = await ingest_event(session, payload)
    await session.commit()
    assert result is not None
    assert result.transition != Transition.NONE
    assert result.deliveries_enqueued >= 1

    rows = list(await session.scalars(select(WebhookDelivery)))
    assert len(rows) >= 1
    row = rows[0]
    assert row.event_name == result.transition.value
    assert row.delivered_at is None
    assert "schema_version" in row.payload


async def test_re_ingest_unchanged_does_not_double_enqueue(
    session, oddsapi_fixture_dir,
) -> None:
    from aggrigator.models import Sport

    await make_sport(session, id="FOOTBALL", name="Football")
    sport = await session.get(Sport, "FOOTBALL")
    await make_league(session, sport=sport, id="NFL", name="NFL", active=True)

    client = FixtureOddsClient(oddsapi_fixture_dir)
    payload = next(p for p in client.get_events(league_id="NFL") if p.get("type") == "match")
    await ingest_event(session, payload)
    await session.commit()
    first = list(await session.scalars(select(WebhookDelivery)))
    await ingest_event(session, payload)
    await session.commit()
    second = list(await session.scalars(select(WebhookDelivery)))
    assert len(first) == len(second), "unchanged ingest should not enqueue again"


# ---- send_one (single-attempt outcome) -------------------------------------


def _make_delivery(event, *, attempts: int = 0) -> WebhookDelivery:
    return WebhookDelivery(
        event_id=event.id,
        event_name="event.finalized",
        idempotency_key=f"{event.id}:abcdef0123456789",
        payload={"schema_version": 1, "event_name": "event.finalized", "event": {}},
        attempts=attempts,
    )


async def test_send_one_success_marks_delivered(session) -> None:
    event = await _seed_finished_event(session)
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = bytes(request.content)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    delivery = _make_delivery(event)
    session.add(delivery)
    await session.commit()

    async with httpx.AsyncClient(transport=transport) as http:
        outcome = await send_one(
            delivery, url=TEST_URL, secret=TEST_SECRET, client=http,
        )
    await session.commit()

    assert outcome.success is True
    assert outcome.permanent_fail is False
    assert delivery.delivered_at is not None
    assert delivery.attempts == 1
    # Receiver-side signature verification works against what we signed.
    verify(secret=TEST_SECRET, body=captured["body"], header_value=captured["headers"][HEADER_NAME.lower()])


async def test_send_one_409_treated_as_success(session) -> None:
    event = await _seed_finished_event(session)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409)

    transport = httpx.MockTransport(handler)
    delivery = _make_delivery(event)
    session.add(delivery)
    await session.commit()

    async with httpx.AsyncClient(transport=transport) as http:
        outcome = await send_one(
            delivery, url=TEST_URL, secret=TEST_SECRET, client=http,
        )

    assert outcome.success is True
    assert delivery.delivered_at is not None


async def test_send_one_5xx_schedules_retry(session) -> None:
    event = await _seed_finished_event(session)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    delivery = _make_delivery(event)
    session.add(delivery)
    await session.commit()

    fixed_now = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)
    async with httpx.AsyncClient(transport=transport) as http:
        outcome = await send_one(
            delivery, url=TEST_URL, secret=TEST_SECRET, client=http,
            now=fixed_now,
        )

    assert outcome.success is False
    assert outcome.permanent_fail is False
    assert delivery.next_retry_at is not None
    expected_seconds = RETRY_BACKOFF_SECONDS[0]
    assert (delivery.next_retry_at - fixed_now).total_seconds() == expected_seconds
    assert delivery.last_status == 503


async def test_send_one_4xx_permanent_fail(session) -> None:
    event = await _seed_finished_event(session)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "schema mismatch"})

    transport = httpx.MockTransport(handler)
    delivery = _make_delivery(event)
    session.add(delivery)
    await session.commit()

    async with httpx.AsyncClient(transport=transport) as http:
        outcome = await send_one(
            delivery, url=TEST_URL, secret=TEST_SECRET, client=http,
        )

    assert outcome.permanent_fail is True
    assert outcome.success is False
    assert delivery.next_retry_at is None
    assert delivery.delivered_at is None
    assert delivery.last_status == 422


async def test_send_one_429_schedules_retry(session) -> None:
    event = await _seed_finished_event(session)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    transport = httpx.MockTransport(handler)
    delivery = _make_delivery(event)
    session.add(delivery)
    await session.commit()

    async with httpx.AsyncClient(transport=transport) as http:
        outcome = await send_one(
            delivery, url=TEST_URL, secret=TEST_SECRET, client=http,
        )

    assert outcome.permanent_fail is False
    assert delivery.next_retry_at is not None


async def test_send_one_exhausts_attempts_then_permanent_fail(session) -> None:
    event = await _seed_finished_event(session)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    delivery = _make_delivery(event, attempts=MAX_ATTEMPTS - 1)
    session.add(delivery)
    await session.commit()

    async with httpx.AsyncClient(transport=transport) as http:
        outcome = await send_one(
            delivery, url=TEST_URL, secret=TEST_SECRET, client=http,
        )

    assert outcome.permanent_fail is True
    assert delivery.attempts == MAX_ATTEMPTS
    assert delivery.next_retry_at is None


async def test_send_one_signs_request_correctly(session) -> None:
    event = await _seed_finished_event(session)
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = bytes(request.content)
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    payload = {
        "schema_version": 1,
        "event_name": "event.finalized",
        "delivery_id": "00000000-0000-0000-0000-000000000000",
        "idempotency_key": f"{event.id}:abc",
        "event": {"event_id": event.id},
    }
    delivery = WebhookDelivery(
        event_id=event.id,
        event_name="event.finalized",
        idempotency_key=f"{event.id}:abc",
        payload=payload,
    )
    session.add(delivery)
    await session.commit()

    async with httpx.AsyncClient(transport=transport) as http:
        await send_one(
            delivery, url=TEST_URL, secret=TEST_SECRET, client=http,
        )

    body = captured["body"]
    assert json.loads(body) == payload
    assert captured["headers"]["x-aggrigator-event-id"] == event.id
    assert captured["headers"]["x-aggrigator-event-name"] == "event.finalized"
    assert captured["headers"]["x-aggrigator-idempotency-key"] == f"{event.id}:abc"
    # The signature itself is verifiable with the same secret.
    verify(
        secret=TEST_SECRET, body=body,
        header_value=captured["headers"][HEADER_NAME.lower()],
    )
