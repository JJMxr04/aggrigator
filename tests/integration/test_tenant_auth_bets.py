"""Tenant-key auth + tenant-scoped bets (plan §6.4 / §6.8 / 6.7 lines).

Covers:
- ``require_tenant_user``: valid / missing / junk / revoked keys.
- §6.4 behavior: tier is NOT checked by the aggregator anymore (FREE keys
  reach analytics; MDProject's Stripe layer gates users now).
- Cross-tenant isolation: user A's key never returns user B's bets.
- The acting-user trusted channel: only the configured service tenant
  may assert ``X-Acting-User``; assertions resolve to the right user.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from aggrigator.config import get_settings
from aggrigator.main import app
from tests.integration.factories import make_tenant_user

pytestmark = pytest.mark.asyncio

_LEAGUES = "/v1/analytics/leagues"
_BETS = "/v1/analytics/bets"


def _key_headers(raw_key: str, acting: uuid.UUID | str | None = None) -> dict:
    h = {"X-Aggrigator-Tenant-Key": raw_key}
    if acting is not None:
        h["X-Acting-User"] = str(acting)
    return h


def _bet_body(label: str = "test bet") -> dict:
    return {
        "label": label,
        "selection_type": "moneyline_home",
        "stake": "10.00",
        "decimal_odds": "2.50",
    }


# ---- require_tenant_user ----------------------------------------------------


async def test_analytics_requires_key(client) -> None:
    assert (await client.get(_LEAGUES)).status_code == 401


async def test_junk_key_is_401(client) -> None:
    r = await client.get(_LEAGUES, headers=_key_headers("agg_test_junkjunkjunk"))
    assert r.status_code == 401


async def test_valid_key_passes_regardless_of_tier(client, session) -> None:
    """§6.4: FREE-tier key reaches analytics — tier gating left the
    aggregator (MDProject decides who sees what before proxying)."""
    _, raw = await make_tenant_user(session, email="free@example.com")  # FREE default
    r = await client.get(_LEAGUES, headers=_key_headers(raw))
    assert r.status_code == 200


async def test_revoked_key_is_401(client, session) -> None:
    from sqlalchemy import update

    from aggrigator.models.tenant import TenantApiKey

    user, raw = await make_tenant_user(session, email="revoked@example.com")
    await session.execute(
        update(TenantApiKey)
        .where(TenantApiKey.tenant_user_id == user.id)
        .values(revoked_at=datetime.now(tz=timezone.utc))
    )
    await session.commit()
    assert (await client.get(_LEAGUES, headers=_key_headers(raw))).status_code == 401


# ---- cross-tenant isolation (6.7 line) --------------------------------------


async def test_user_a_never_sees_user_b_bets(client, session) -> None:
    _, key_a = await make_tenant_user(session, email="a@example.com")
    _, key_b = await make_tenant_user(session, email="b@example.com")

    r = await client.post(_BETS, headers=_key_headers(key_b), json=_bet_body("b's bet"))
    assert r.status_code == 201
    b_bet_id = r.json()["id"]

    r = await client.get(_BETS, headers=_key_headers(key_a))
    assert r.status_code == 200
    assert r.json() == []

    # Direct id probe is a 404, indistinguishable from nonexistence.
    r = await client.patch(
        f"{_BETS}/{b_bet_id}", headers=_key_headers(key_a), json={"note": "mine now"},
    )
    assert r.status_code == 404
    assert (await client.delete(
        f"{_BETS}/{b_bet_id}", headers=_key_headers(key_a),
    )).status_code == 404


# ---- acting-user trusted channel (§6.4 / D-1) -------------------------------


@pytest.fixture
def _service_settings(monkeypatch):
    """Designate a known external id as THE service tenant via settings
    override — mirrors setting AGG_SERVICE_TENANT_EXTERNAL_ID in Coolify."""
    service_external_id = uuid.uuid4()
    base = get_settings()
    patched = base.model_copy(
        update={"service_tenant_external_id": str(service_external_id)}
    )
    app.dependency_overrides[get_settings] = lambda: patched
    yield service_external_id
    app.dependency_overrides.pop(get_settings, None)


async def test_service_key_asserts_acting_user(client, session, _service_settings) -> None:
    service_external_id = _service_settings
    _, service_key = await make_tenant_user(
        session, email="service@example.com", external_user_id=service_external_id,
    )
    end_user, _ = await make_tenant_user(session, email="enduser@example.com")

    # Service key creates + reads bets FOR the end user.
    r = await client.post(
        _BETS,
        headers=_key_headers(service_key, acting=end_user.external_user_id),
        json=_bet_body("asserted bet"),
    )
    assert r.status_code == 201
    # Attribution check on the row itself — BetOut doesn't expose the
    # tenant id (6.7: attributes to the acting user, NOT the service tenant).
    from aggrigator.models import Bet

    row = await session.get(Bet, r.json()["id"])
    assert row.tenant_user_id == end_user.id

    r = await client.get(
        _BETS, headers=_key_headers(service_key, acting=end_user.external_user_id),
    )
    assert [b["label"] for b in r.json()] == ["asserted bet"]

    # Without the header, the service key acts as itself → sees nothing.
    r = await client.get(_BETS, headers=_key_headers(service_key))
    assert r.json() == []


async def test_non_service_key_cannot_assert(client, session, _service_settings) -> None:
    _, key_a = await make_tenant_user(session, email="a2@example.com")
    user_b, _ = await make_tenant_user(session, email="b2@example.com")

    r = await client.get(
        _BETS, headers=_key_headers(key_a, acting=user_b.external_user_id),
    )
    assert r.status_code == 403


async def test_asserting_yourself_is_allowed_for_any_key(client, session) -> None:
    user_a, key_a = await make_tenant_user(session, email="self@example.com")
    r = await client.get(
        _BETS, headers=_key_headers(key_a, acting=user_a.external_user_id),
    )
    assert r.status_code == 200


async def test_service_key_asserting_unknown_user_is_404(client, session, _service_settings) -> None:
    service_external_id = _service_settings
    _, service_key = await make_tenant_user(
        session, email="service2@example.com", external_user_id=service_external_id,
    )
    r = await client.get(
        _BETS, headers=_key_headers(service_key, acting=uuid.uuid4()),
    )
    assert r.status_code == 404


async def test_no_service_tenant_configured_means_no_assertions(client, session) -> None:
    """Default settings: AGG_SERVICE_TENANT_EXTERNAL_ID empty → 403 for
    every cross-user assertion (per-user keys keep working unchanged)."""
    _, key_a = await make_tenant_user(session, email="a3@example.com")
    r = await client.get(_BETS, headers=_key_headers(key_a, acting=uuid.uuid4()))
    assert r.status_code == 403


async def test_malformed_acting_header_is_422(client, session) -> None:
    _, key_a = await make_tenant_user(session, email="a4@example.com")
    r = await client.get(_BETS, headers=_key_headers(key_a, acting="not-a-uuid"))
    assert r.status_code == 422
