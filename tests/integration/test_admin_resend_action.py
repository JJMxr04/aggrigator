"""Admin "Resend" bulk action on WebhookDeliveryView.

The action takes selected *delivery* row ids, resolves each to its parent
event, and force-enqueues a fresh delivery per unique event (mirroring the
EventView "Re-enqueue webhook" action, but driven from the delivery list and
deduplicated by event).

These call the action method directly with a hand-built Starlette ``Request``
rather than going through SQLAdmin's HTTP routing + admin auth — the new logic
is delivery→event resolution + dedupe, which the DB-backed assertions cover.
The action opens its own ``async_session_factory()`` session; the root
conftest mirrors ``AGG_TEST_DATABASE_URL`` into ``AGG_DATABASE_URL`` so that
session targets the same Postgres the test fixtures write to.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from starlette.requests import Request

from aggrigator.admin.views import WebhookDeliveryView
from aggrigator.config import get_settings
from aggrigator.models import WebhookDelivery
from tests.integration.factories import (
    make_event,
    make_league,
    make_market,
    make_selection,
    make_sport,
    make_team,
)

pytestmark = pytest.mark.asyncio


TEST_BASE_URL = "https://example.com"


@pytest.fixture(autouse=True)
def _set_webhook_target():
    """force_enqueue_for_event returns None unless a target URL is set."""
    s = get_settings()
    original = s.mdproject_url
    s.mdproject_url = TEST_BASE_URL
    yield
    s.mdproject_url = original


def _make_request(pks: str, *, referer: str = "http://testserver/admin/webhook-delivery/list") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/admin/webhook-delivery/action",
        "query_string": f"pks={pks}".encode(),
        "headers": [(b"referer", referer.encode())],
        "session": {},
    }
    return Request(scope)


def _messages(request: Request) -> list[dict]:
    return request.scope["session"].get("_messages", [])


async def _resend(request: Request) -> None:
    # Call the undecorated action body, bypassing SQLAdmin's login_required
    # wrapper (which needs a live router/auth session this hand-built request
    # doesn't have). functools.wraps exposes the original via __wrapped__.
    await WebhookDeliveryView.resend.__wrapped__(WebhookDeliveryView(), request)


async def _seed_event(session, *, status_type: str, suffix: str = ""):
    sport = await make_sport(session, id="FOOTBALL", name="Football")
    league = await make_league(session, sport=sport, id="NFL", name="NFL", active=True)
    home = await make_team(session, league=league, team_id=f"DAL{suffix}")
    away = await make_team(session, league=league, team_id=f"PHI{suffix}", name_long="Philly")
    event = await make_event(
        session, league=league, home_team=home, away_team=away,
        id=f"evt-{status_type}{suffix}",
        status_type=status_type,
        is_finalized=(status_type == "finished"),
        completed=(status_type == "finished"),
        home_score=27, away_score=24, winner_code=1,
    )
    market = await make_market(session, event=event, type="NFL_POINTS_ML")
    await make_selection(session, market=market, type="HOME")
    return event


def _make_delivery(session, event, *, key_suffix: str) -> WebhookDelivery:
    row = WebhookDelivery(
        event_id=event.id,
        event_name="event.finalized",
        idempotency_key=f"{event.id}:{key_suffix}",
        payload={"schema_version": 1, "event_name": "event.finalized", "event": {}},
    )
    session.add(row)
    return row


async def test_resend_creates_fresh_delivery_for_event(session, monkeypatch):
    calls: list[int] = []

    async def _spy() -> bool:
        calls.append(1)
        return True

    monkeypatch.setattr("aggrigator.admin.views.notify_webhook_worker", _spy)

    event = await _seed_event(session, status_type="finished")
    original = _make_delivery(session, event, key_suffix="orig")
    await session.commit()
    original_id = original.id

    request = _make_request(str(original_id))
    await _resend(request)

    rows = list(await session.scalars(select(WebhookDelivery)))
    assert len(rows) == 2, "a fresh delivery should be created alongside the original"
    fresh = [r for r in rows if r.id != original_id]
    assert len(fresh) == 1
    assert ":manual:" in fresh[0].idempotency_key
    assert fresh[0].event_id == event.id
    assert calls == [1], "worker notified exactly once"
    assert any(m["category"] == "success" for m in _messages(request))


async def test_resend_dedupes_by_event(session, monkeypatch):
    calls: list[int] = []

    async def _spy() -> bool:
        calls.append(1)
        return True

    monkeypatch.setattr("aggrigator.admin.views.notify_webhook_worker", _spy)

    event = await _seed_event(session, status_type="finished")
    d1 = _make_delivery(session, event, key_suffix="a")
    d2 = _make_delivery(session, event, key_suffix="b")
    await session.commit()
    pks = f"{d1.id},{d2.id}"

    request = _make_request(pks)
    await _resend(request)

    rows = list(await session.scalars(select(WebhookDelivery)))
    assert len(rows) == 3, "two deliveries of one event resend as one fresh row"
    assert calls == [1]


async def test_resend_skips_non_terminal_event(session, monkeypatch):
    calls: list[int] = []

    async def _spy() -> bool:
        calls.append(1)
        return True

    monkeypatch.setattr("aggrigator.admin.views.notify_webhook_worker", _spy)

    event = await _seed_event(session, status_type="notstarted")
    delivery = _make_delivery(session, event, key_suffix="orig")
    await session.commit()

    request = _make_request(str(delivery.id))
    await _resend(request)

    rows = list(await session.scalars(select(WebhookDelivery)))
    assert len(rows) == 1, "non-terminal event should not produce a fresh delivery"
    assert calls == [], "worker not notified when nothing enqueued"
    assert any(m["category"] == "warning" for m in _messages(request))


async def test_resend_reports_missing_delivery(session, monkeypatch):
    calls: list[int] = []

    async def _spy() -> bool:
        calls.append(1)
        return True

    monkeypatch.setattr("aggrigator.admin.views.notify_webhook_worker", _spy)

    import uuid
    bogus = uuid.uuid4()
    request = _make_request(str(bogus))
    await _resend(request)

    rows = list(await session.scalars(select(WebhookDelivery)))
    assert len(rows) == 0
    assert calls == []
    assert any(m["category"] == "danger" for m in _messages(request))
