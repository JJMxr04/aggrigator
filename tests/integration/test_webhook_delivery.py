"""End-to-end webhook delivery tests.

Covers:
- ingest pipeline produces a WebhookDelivery row when transition != NONE
- enqueue is idempotent on unchanged event state (no duplicate row)
- send_one applies the §4.7 retry/success/permanent-fail rules
- the receiver-side signature verification works against what we sign
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from aggrigator.ingest.lifecycle import Transition
from aggrigator.ingest.orchestrator import ingest_event
from aggrigator.ingest.sgo_simulator import FixtureSgoClient
from aggrigator.models import WebhookDelivery, WebhookEndpoint
from aggrigator.security.webhook_signing import HEADER_NAME, verify
from aggrigator.webhooks.deliver import (
    MAX_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    send_one,
)
from tests.integration.factories import (
    login_and_get_token,
    make_event,
    make_league,
    make_market,
    make_selection,
    make_sport,
    make_team,
)

pytestmark = pytest.mark.asyncio


# ---- helpers ---------------------------------------------------------------


async def _create_endpoint(
    client, *, events: list[str], url: str = "https://example.com/hook",
) -> tuple[str, str]:
    token = await login_and_get_token(client)
    auth = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/v1/webhook-endpoints",
        json={"url": url, "events": events},
        headers=auth,
    )
    return r.json()["id"], r.json()["signing_secret"]


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


async def test_ingest_enqueues_delivery_for_subscribed_endpoint(
    client, session, sgo_fixture_dir,
) -> None:
    eid, _ = await _create_endpoint(client, events=["event.finalized", "event.voided", "event.lifecycle.changed"])
    await make_sport(session, id="FOOTBALL", name="Football")
    await make_league(
        session, sport=(await session.get(__import__("aggrigator").models.Sport, "FOOTBALL")),
        id="NFL", name="NFL", active=True,
    )

    sgo = FixtureSgoClient(sgo_fixture_dir)
    payload = next(p for p in sgo.get_events(league_id="NFL") if p.get("type") == "match")
    result = await ingest_event(session, payload)
    await session.commit()
    assert result is not None
    assert result.transition != Transition.NONE
    assert result.deliveries_enqueued >= 1

    rows = list(await session.scalars(
        select(WebhookDelivery).where(WebhookDelivery.endpoint_id == uuid.UUID(eid))
    ))
    assert len(rows) >= 1
    row = rows[0]
    assert row.event_name == result.transition.value
    assert row.delivered_at is None
    assert "schema_version" in row.payload


async def test_ingest_does_not_enqueue_when_endpoint_does_not_subscribe(
    client, session, sgo_fixture_dir,
) -> None:
    # Endpoint subscribed only to a hypothetical event we won't emit.
    eid, _ = await _create_endpoint(client, events=["selection.settled"])
    from aggrigator.models import Sport

    await make_sport(session, id="FOOTBALL", name="Football")
    sport = await session.get(Sport, "FOOTBALL")
    await make_league(session, sport=sport, id="NFL", name="NFL", active=True)

    sgo = FixtureSgoClient(sgo_fixture_dir)
    payload = next(p for p in sgo.get_events(league_id="NFL") if p.get("type") == "match")
    result = await ingest_event(session, payload)
    await session.commit()
    assert result is not None
    assert result.deliveries_enqueued == 0


async def test_re_ingest_unchanged_does_not_double_enqueue(
    client, session, sgo_fixture_dir,
) -> None:
    eid, _ = await _create_endpoint(
        client, events=["event.finalized", "event.voided", "event.lifecycle.changed"],
    )
    from aggrigator.models import Sport

    await make_sport(session, id="FOOTBALL", name="Football")
    sport = await session.get(Sport, "FOOTBALL")
    await make_league(session, sport=sport, id="NFL", name="NFL", active=True)

    sgo = FixtureSgoClient(sgo_fixture_dir)
    payload = next(p for p in sgo.get_events(league_id="NFL") if p.get("type") == "match")
    await ingest_event(session, payload)
    await session.commit()
    first = list(await session.scalars(
        select(WebhookDelivery).where(WebhookDelivery.endpoint_id == uuid.UUID(eid))
    ))
    await ingest_event(session, payload)
    await session.commit()
    second = list(await session.scalars(
        select(WebhookDelivery).where(WebhookDelivery.endpoint_id == uuid.UUID(eid))
    ))
    assert len(first) == len(second), "unchanged ingest should not enqueue again"


# ---- send_one (single-attempt outcome) -------------------------------------


async def test_send_one_success_marks_delivered(client, session) -> None:
    eid, secret = await _create_endpoint(client, events=["event.finalized"])
    event = await _seed_finished_event(session)
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = bytes(request.content)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    delivery = WebhookDelivery(
        endpoint_id=uuid.UUID(eid),
        event_id=event.id,
        event_name="event.finalized",
        idempotency_key=f"{event.id}:abcdef0123456789",
        payload={"schema_version": 1, "event_name": "event.finalized", "event": {}},
    )
    session.add(delivery)
    await session.commit()

    async with httpx.AsyncClient(transport=transport) as http:
        outcome = await send_one(
            delivery, url="https://example.com/hook", secret=secret, client=http,
        )
    await session.commit()

    assert outcome.success is True
    assert outcome.permanent_fail is False
    assert delivery.delivered_at is not None
    assert delivery.attempts == 1
    # Receiver-side signature verification works against what we signed.
    verify(secret=secret, body=captured["body"], header_value=captured["headers"][HEADER_NAME.lower()])


async def test_send_one_409_treated_as_success(client, session) -> None:
    eid, secret = await _create_endpoint(client, events=["event.finalized"])
    event = await _seed_finished_event(session)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409)

    transport = httpx.MockTransport(handler)
    delivery = WebhookDelivery(
        endpoint_id=uuid.UUID(eid),
        event_id=event.id,
        event_name="event.finalized",
        idempotency_key=f"{event.id}:abcdef0123456789",
        payload={"schema_version": 1, "event_name": "event.finalized", "event": {}},
    )
    session.add(delivery)
    await session.commit()

    async with httpx.AsyncClient(transport=transport) as http:
        outcome = await send_one(
            delivery, url="https://example.com/hook", secret=secret, client=http,
        )

    assert outcome.success is True
    assert delivery.delivered_at is not None


async def test_send_one_5xx_schedules_retry(client, session) -> None:
    eid, secret = await _create_endpoint(client, events=["event.finalized"])
    event = await _seed_finished_event(session)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    delivery = WebhookDelivery(
        endpoint_id=uuid.UUID(eid),
        event_id=event.id,
        event_name="event.finalized",
        idempotency_key=f"{event.id}:abcdef0123456789",
        payload={"schema_version": 1, "event_name": "event.finalized", "event": {}},
    )
    session.add(delivery)
    await session.commit()

    fixed_now = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)
    async with httpx.AsyncClient(transport=transport) as http:
        outcome = await send_one(
            delivery, url="https://example.com/hook", secret=secret, client=http,
            now=fixed_now,
        )

    assert outcome.success is False
    assert outcome.permanent_fail is False
    assert delivery.next_retry_at is not None
    expected_seconds = RETRY_BACKOFF_SECONDS[0]
    assert (delivery.next_retry_at - fixed_now).total_seconds() == expected_seconds
    assert delivery.last_status == 503


async def test_send_one_4xx_permanent_fail(client, session) -> None:
    eid, secret = await _create_endpoint(client, events=["event.finalized"])
    event = await _seed_finished_event(session)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "schema mismatch"})

    transport = httpx.MockTransport(handler)
    delivery = WebhookDelivery(
        endpoint_id=uuid.UUID(eid),
        event_id=event.id,
        event_name="event.finalized",
        idempotency_key=f"{event.id}:abcdef0123456789",
        payload={"schema_version": 1, "event_name": "event.finalized", "event": {}},
    )
    session.add(delivery)
    await session.commit()

    async with httpx.AsyncClient(transport=transport) as http:
        outcome = await send_one(
            delivery, url="https://example.com/hook", secret=secret, client=http,
        )

    assert outcome.permanent_fail is True
    assert outcome.success is False
    assert delivery.next_retry_at is None
    assert delivery.delivered_at is None
    assert delivery.last_status == 422


async def test_send_one_429_schedules_retry(client, session) -> None:
    eid, secret = await _create_endpoint(client, events=["event.finalized"])
    event = await _seed_finished_event(session)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    transport = httpx.MockTransport(handler)
    delivery = WebhookDelivery(
        endpoint_id=uuid.UUID(eid),
        event_id=event.id,
        event_name="event.finalized",
        idempotency_key=f"{event.id}:abcdef0123456789",
        payload={"schema_version": 1, "event_name": "event.finalized", "event": {}},
    )
    session.add(delivery)
    await session.commit()

    async with httpx.AsyncClient(transport=transport) as http:
        outcome = await send_one(
            delivery, url="https://example.com/hook", secret=secret, client=http,
        )

    assert outcome.permanent_fail is False
    assert delivery.next_retry_at is not None


async def test_send_one_exhausts_attempts_then_permanent_fail(client, session) -> None:
    eid, secret = await _create_endpoint(client, events=["event.finalized"])
    event = await _seed_finished_event(session)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    delivery = WebhookDelivery(
        endpoint_id=uuid.UUID(eid),
        event_id=event.id,
        event_name="event.finalized",
        idempotency_key=f"{event.id}:abcdef0123456789",
        payload={"schema_version": 1, "event_name": "event.finalized", "event": {}},
        attempts=MAX_ATTEMPTS - 1,  # one short of max
    )
    session.add(delivery)
    await session.commit()

    async with httpx.AsyncClient(transport=transport) as http:
        outcome = await send_one(
            delivery, url="https://example.com/hook", secret=secret, client=http,
        )

    assert outcome.permanent_fail is True
    assert delivery.attempts == MAX_ATTEMPTS
    assert delivery.next_retry_at is None


async def test_send_one_signs_request_correctly(client, session) -> None:
    eid, secret = await _create_endpoint(client, events=["event.finalized"])
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
        endpoint_id=uuid.UUID(eid),
        event_id=event.id,
        event_name="event.finalized",
        idempotency_key=f"{event.id}:abc",
        payload=payload,
    )
    session.add(delivery)
    await session.commit()

    async with httpx.AsyncClient(transport=transport) as http:
        await send_one(
            delivery, url="https://example.com/hook", secret=secret, client=http,
        )

    body = captured["body"]
    assert json.loads(body) == payload
    assert captured["headers"]["x-aggrigator-event-id"] == event.id
    assert captured["headers"]["x-aggrigator-event-name"] == "event.finalized"
    assert captured["headers"]["x-aggrigator-idempotency-key"] == f"{event.id}:abc"
    # The signature itself is verifiable with the same secret.
    verify(
        secret=secret, body=body,
        header_value=captured["headers"][HEADER_NAME.lower()],
    )
