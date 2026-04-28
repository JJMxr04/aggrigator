"""Tests for the ARQ task functions.

We don't actually run ARQ here (that would need a Redis fixture); the task
functions are plain async functions, so we call them directly to prove they
do the right side-effects against a real DB. The ARQ scheduling mechanism is
trusted to call them on its cron.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from aggrigator.ingest.lifecycle import Transition
from aggrigator.ingest.orchestrator import ingest_event
from aggrigator.ingest.sgo_simulator import FixtureSgoClient
from aggrigator.models import Sport, WebhookDelivery, WebhookEndpoint
from aggrigator.security.webhook_signing import (
    encrypt_secret,
    fernet_key_from_passphrase,
)
from aggrigator.workers.tasks.webhook_deliver import run_deliver_due
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


# ---- seeder ---------------------------------------------------------------


async def test_seed_sports_and_leagues_against_fixture(
    session, sgo_fixture_dir: Path,
) -> None:
    from aggrigator.ingest.seed import seed_all

    client = FixtureSgoClient(sgo_fixture_dir)
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


async def test_run_deliver_due_drains_pending(client, session, monkeypatch) -> None:
    """Plant a delivery row, point it at a mock-handler URL, run the worker."""
    # Use a deterministic key so the worker can decrypt what we encrypt here.
    key = fernet_key_from_passphrase("worker-test")
    monkeypatch.setattr(
        "aggrigator.workers.tasks.webhook_deliver.get_settings",
        lambda: type("S", (), {"secret_encryption_key": key})(),
    )

    event = await _seed_event_with_market(session)

    endpoint = WebhookEndpoint(
        user_id=__import__("uuid").uuid4(),
        url="https://example.test/hook",
        secret_ciphertext=encrypt_secret("worker-secret", key=key),
        events=["event.finalized"],
        enabled=True,
    )
    # WebhookEndpoint.user_id has FK to auth_user; route around it for this
    # test by inserting a User first.
    from aggrigator.models import User
    from aggrigator.security.passwords import hash_password
    user = User(email="hook@example.com", password_hash=hash_password("xx12345678"))
    session.add(user)
    await session.flush()
    endpoint.user_id = user.id
    session.add(endpoint)
    await session.flush()

    delivery = WebhookDelivery(
        endpoint_id=endpoint.id,
        event_id=event.id,
        event_name="event.finalized",
        idempotency_key=f"{event.id}:abcd",
        payload={"schema_version": 1, "event": {"event_id": event.id}},
    )
    session.add(delivery)
    await session.commit()

    # Patch the AsyncClient inside the worker so we don't need the network.
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)

    class _PatchedClient(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    monkeypatch.setattr(
        "aggrigator.workers.tasks.webhook_deliver.httpx.AsyncClient",
        _PatchedClient,
    )

    summary = await run_deliver_due()
    assert summary["sent"] == 1

    # Re-fetch to confirm delivered_at was set.
    refreshed = await session.get(WebhookDelivery, delivery.id)
    assert refreshed is not None
    await session.refresh(refreshed)
    assert refreshed.delivered_at is not None


async def test_run_deliver_due_skips_disabled_endpoint(
    client, session, monkeypatch,
) -> None:
    key = fernet_key_from_passphrase("worker-test")
    monkeypatch.setattr(
        "aggrigator.workers.tasks.webhook_deliver.get_settings",
        lambda: type("S", (), {"secret_encryption_key": key})(),
    )

    event = await _seed_event_with_market(session)

    from aggrigator.models import User
    from aggrigator.security.passwords import hash_password
    user = User(email="disabled@example.com", password_hash=hash_password("xx12345678"))
    session.add(user)
    await session.flush()
    endpoint = WebhookEndpoint(
        user_id=user.id,
        url="https://example.test/hook",
        secret_ciphertext=encrypt_secret("s", key=key),
        events=["event.finalized"],
        enabled=False,  # disabled
    )
    session.add(endpoint)
    await session.flush()
    delivery = WebhookDelivery(
        endpoint_id=endpoint.id, event_id=event.id,
        event_name="event.finalized",
        idempotency_key=f"{event.id}:disabled-test",
        payload={"schema_version": 1, "event": {"event_id": event.id}},
    )
    session.add(delivery)
    await session.commit()

    summary = await run_deliver_due()
    # Disabled endpoint counts as a permanent fail rather than a real send.
    assert summary == {"sent": 0, "retried": 0, "failed": 1}

    refreshed = await session.get(WebhookDelivery, delivery.id)
    await session.refresh(refreshed)
    assert refreshed.delivered_at is None
    assert refreshed.last_error is not None
    assert "endpoint deleted or disabled" in refreshed.last_error


async def test_run_deliver_due_retries_on_5xx(client, session, monkeypatch) -> None:
    key = fernet_key_from_passphrase("worker-test")
    monkeypatch.setattr(
        "aggrigator.workers.tasks.webhook_deliver.get_settings",
        lambda: type("S", (), {"secret_encryption_key": key})(),
    )

    event = await _seed_event_with_market(session)

    from aggrigator.models import User
    from aggrigator.security.passwords import hash_password
    user = User(email="retry@example.com", password_hash=hash_password("xx12345678"))
    session.add(user)
    await session.flush()
    endpoint = WebhookEndpoint(
        user_id=user.id,
        url="https://example.test/hook",
        secret_ciphertext=encrypt_secret("s", key=key),
        events=["event.finalized"],
        enabled=True,
    )
    session.add(endpoint)
    await session.flush()
    delivery = WebhookDelivery(
        endpoint_id=endpoint.id, event_id=event.id,
        event_name="event.finalized",
        idempotency_key=f"{event.id}:retry-test",
        payload={"schema_version": 1, "event": {"event_id": event.id}},
    )
    session.add(delivery)
    await session.commit()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)

    class _PatchedClient(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    monkeypatch.setattr(
        "aggrigator.workers.tasks.webhook_deliver.httpx.AsyncClient",
        _PatchedClient,
    )

    summary = await run_deliver_due()
    assert summary == {"sent": 0, "retried": 1, "failed": 0}
    refreshed = await session.get(WebhookDelivery, delivery.id)
    await session.refresh(refreshed)
    assert refreshed.delivered_at is None
    assert refreshed.next_retry_at is not None
