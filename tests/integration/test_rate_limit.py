"""Integration tests for security/rate_limit — plan §6.2 P0-1 + §6.7.

Storage is in-process memory here (no AGG_RATELIMIT_STORAGE_URL in the
test env); the autouse conftest fixture resets counters between tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aggrigator.models.audit import AuditLog
from aggrigator.security import rate_limit
from aggrigator.security.audit import Event
from tests.integration.factories import make_user

pytestmark = pytest.mark.asyncio

_LOGIN = "/v1/auth/login"


async def _attempt(client, email="nobody@example.com", password="wrong"):
    return await client.post(_LOGIN, json={"email": email, "password": password})


async def test_sixth_login_attempt_in_a_minute_is_429(client) -> None:
    for _ in range(5):
        r = await _attempt(client)
        assert r.status_code == 401
    r = await _attempt(client)
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers}
    assert int(r.headers["Retry-After"]) >= 1


async def test_429_identical_for_existing_and_missing_accounts(client, session) -> None:
    """The limiter must not become an account oracle (§6.7)."""
    await make_user(session, email="real@example.com")

    bodies = []
    for email in ("real@example.com", "ghost@example.com"):
        await rate_limit.reset_for_tests()
        for _ in range(5):
            await _attempt(client, email=email)
        r = await _attempt(client, email=email)
        assert r.status_code == 429
        bodies.append(r.json())
    assert bodies[0] == bodies[1]


async def test_refresh_endpoint_is_limited(client) -> None:
    for _ in range(5):
        r = await client.post("/v1/auth/refresh", json={"refresh_token": "junk"})
        assert r.status_code == 401
    r = await client.post("/v1/auth/refresh", json={"refresh_token": "junk"})
    assert r.status_code == 429


async def test_per_account_backoff_trips_across_window(client, session) -> None:
    """10 accumulated AUTH_LOGIN_FAIL rows for one email → 429 on the next
    attempt even when the per-IP budget is untouched."""
    email = "target@example.com"
    now = datetime.now(tz=timezone.utc)
    for i in range(rate_limit.ACCOUNT_BACKOFF_MAX_FAILS):
        session.add(AuditLog(
            event_name=Event.AUTH_LOGIN_FAIL,
            ip=f"10.0.0.{i}",  # distributed sources — per-IP can't see this
            audit_metadata={"email": email},
            created_at=now - timedelta(minutes=1),
        ))
    await session.commit()

    r = await _attempt(client, email=email)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1

    # Other accounts are unaffected.
    r = await _attempt(client, email="other@example.com")
    assert r.status_code == 401


async def test_admin_login_form_is_limited(client) -> None:
    for _ in range(5):
        r = await client.post(
            "/admin/login", data={"username": "x@x.com", "password": "wrong"},
        )
        assert r.status_code != 429
    r = await client.post(
        "/admin/login", data={"username": "x@x.com", "password": "wrong"},
    )
    assert r.status_code == 429


async def test_public_tier_allows_120_then_429(client) -> None:
    for _ in range(120):
        r = await client.get("/v1/sports")
        assert r.status_code == 200
    r = await client.get("/v1/sports")
    assert r.status_code == 429


async def test_keyed_requests_ride_their_own_bucket(client, session) -> None:
    """A tenant-key header buys a separate (bigger) bucket — exhausting the
    anonymous tier must not starve keyed traffic, and vice versa."""
    from tests.integration.factories import make_tenant_user

    _, raw = await make_tenant_user(session, email="bucket@example.com")
    for _ in range(120):
        await client.get("/v1/sports")
    assert (await client.get("/v1/sports")).status_code == 429
    r = await client.get(
        "/v1/sports", headers={"X-Aggrigator-Tenant-Key": raw},
    )
    assert r.status_code == 200


async def test_health_probes_are_exempt(client) -> None:
    for _ in range(120):
        await client.get("/v1/sports")
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).status_code == 200
