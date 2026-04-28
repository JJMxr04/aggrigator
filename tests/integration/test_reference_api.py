"""Tests for /v1/sports, /v1/leagues, /v1/bookmakers."""

from __future__ import annotations

import pytest

from tests.integration.factories import (
    login_and_get_token,
    make_bookmaker,
    make_league,
    make_sport,
)

pytestmark = pytest.mark.asyncio


async def test_list_sports_requires_auth(client) -> None:
    r = await client.get("/v1/sports")
    assert r.status_code == 401


async def test_list_sports_returns_seeded(client, session) -> None:
    fb = await make_sport(session, id="FOOTBALL", name="Football", active=True)
    bb = await make_sport(session, id="BASKETBALL", name="Basketball", active=False)

    token = await login_and_get_token(client)
    r = await client.get("/v1/sports", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()]
    assert fb.id in ids
    assert bb.id in ids


async def test_list_sports_filter_active(client, session) -> None:
    await make_sport(session, id="FOOTBALL", name="Football", active=True)
    await make_sport(session, id="BASKETBALL", name="Basketball", active=False)
    token = await login_and_get_token(client)
    r = await client.get(
        "/v1/sports?active=true", headers={"Authorization": f"Bearer {token}"}
    )
    ids = [row["id"] for row in r.json()]
    assert ids == ["FOOTBALL"]


async def test_list_leagues_filter_by_sport(client, session) -> None:
    fb = await make_sport(session, id="FOOTBALL", name="Football")
    bb = await make_sport(session, id="BASKETBALL", name="Basketball")
    await make_league(session, sport=fb, id="NFL", name="NFL")
    await make_league(session, sport=bb, id="NBA", name="NBA")

    token = await login_and_get_token(client)
    r = await client.get(
        "/v1/leagues?sport_id=FOOTBALL", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()]
    assert ids == ["NFL"]


async def test_list_bookmakers(client, session) -> None:
    await make_bookmaker(session, id="DRAFTKINGS", name="DraftKings", active=True)
    await make_bookmaker(session, id="OLD", name="Old Book", active=False)

    token = await login_and_get_token(client)
    r = await client.get(
        "/v1/bookmakers?active=true", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()]
    assert ids == ["DRAFTKINGS"]
