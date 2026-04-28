"""Tests for /v1/selections/{id}/movement and /v1/slips."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tests.integration.factories import (
    login_and_get_token,
    make_event,
    make_league,
    make_market,
    make_quote,
    make_selection,
    make_sport,
    make_team,
)

pytestmark = pytest.mark.asyncio


async def _seed_selection(session, *, decimal_odds=Decimal("1.50")):
    sport = await make_sport(session, id="FOOTBALL", name="Football")
    league = await make_league(session, sport=sport, id="NFL", name="NFL")
    home = await make_team(session, league=league, team_id="DAL")
    away = await make_team(session, league=league, team_id="PHI", name_long="Philly")
    event = await make_event(session, league=league, home_team=home, away_team=away)
    market = await make_market(session, event=event, type="NFL_POINTS_ML")
    return await make_selection(
        session, market=market, type="HOME", decimal_odds=decimal_odds
    )


async def test_movement_404(client) -> None:
    token = await login_and_get_token(client)
    r = await client.get(
        "/v1/selections/missing/movement",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


async def test_movement_returns_quotes_within_window(client, session) -> None:
    sel = await _seed_selection(session)
    now = datetime.now(tz=timezone.utc)
    await make_quote(session, selection=sel, decimal_odds=Decimal("1.40"), captured_at=now - timedelta(hours=2))
    await make_quote(session, selection=sel, decimal_odds=Decimal("1.45"), captured_at=now - timedelta(hours=1))
    await make_quote(session, selection=sel, decimal_odds=Decimal("1.50"), captured_at=now)

    token = await login_and_get_token(client)
    r = await client.get(
        f"/v1/selections/{sel.id}/movement",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["selection_id"] == sel.id
    assert len(body["quotes"]) == 3
    assert [float(q["decimal_odds"]) for q in body["quotes"]] == [1.40, 1.45, 1.50]


async def test_movement_filters_with_since(client, session) -> None:
    sel = await _seed_selection(session)
    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(days=7)
    await make_quote(session, selection=sel, decimal_odds=Decimal("1.40"), captured_at=old)
    await make_quote(session, selection=sel, decimal_odds=Decimal("1.50"), captured_at=now)

    token = await login_and_get_token(client)
    cutoff = (now - timedelta(hours=1)).isoformat()
    r = await client.get(
        f"/v1/selections/{sel.id}/movement",
        params={"since": cutoff},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["quotes"]) == 1


async def test_slips_combines_decimals(client, session) -> None:
    sport = await make_sport(session, id="FOOTBALL", name="Football")
    league = await make_league(session, sport=sport, id="NFL", name="NFL")
    home = await make_team(session, league=league, team_id="DAL")
    away = await make_team(session, league=league, team_id="PHI", name_long="Philly")
    event = await make_event(session, league=league, home_team=home, away_team=away)
    market = await make_market(session, event=event, type="NFL_POINTS_ML")
    s1 = await make_selection(session, market=market, type="HOME", decimal_odds=Decimal("1.5000"))
    s2 = await make_selection(session, market=market, type="AWAY", decimal_odds=Decimal("2.0000"))

    token = await login_and_get_token(client)
    r = await client.post(
        "/v1/slips",
        json={"legs": [{"selection_id": s1.id}, {"selection_id": s2.id}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201
    body = r.json()
    assert len(body["legs"]) == 2
    assert Decimal(body["combined_decimal"]) == Decimal("3.0000")


async def test_slips_rejects_unknown_selection(client, session) -> None:
    token = await login_and_get_token(client)
    r = await client.post(
        "/v1/slips",
        json={"legs": [{"selection_id": "does-not-exist"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
