"""Tests for /v1/sports, /v1/leagues, /v1/bookmakers — public endpoints."""

from __future__ import annotations

import pytest

from tests.integration.factories import (
    make_bookmaker,
    make_league,
    make_sport,
)

pytestmark = pytest.mark.asyncio


async def test_list_sports_is_public(client) -> None:
    r = await client.get("/v1/sports")
    assert r.status_code == 200


async def test_list_sports_returns_seeded(client, session) -> None:
    fb = await make_sport(session, id="FOOTBALL", name="Football", active=True)
    bb = await make_sport(session, id="BASKETBALL", name="Basketball", active=False)

    r = await client.get("/v1/sports")
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()]
    assert fb.id in ids
    assert bb.id in ids


async def test_list_sports_filter_active(client, session) -> None:
    await make_sport(session, id="FOOTBALL", name="Football", active=True)
    await make_sport(session, id="BASKETBALL", name="Basketball", active=False)
    r = await client.get("/v1/sports?active=true")
    ids = [row["id"] for row in r.json()]
    assert ids == ["FOOTBALL"]


async def test_list_leagues_filter_by_sport(client, session) -> None:
    fb = await make_sport(session, id="FOOTBALL", name="Football")
    bb = await make_sport(session, id="BASKETBALL", name="Basketball")
    await make_league(session, sport=fb, id="NFL", name="NFL")
    await make_league(session, sport=bb, id="NBA", name="NBA")

    r = await client.get("/v1/leagues?sport_id=FOOTBALL")
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()]
    assert ids == ["NFL"]


async def test_list_bookmakers(client, session) -> None:
    await make_bookmaker(session, id="DRAFTKINGS", name="DraftKings", active=True)
    await make_bookmaker(session, id="OLD", name="Old Book", active=False)

    r = await client.get("/v1/bookmakers?active=true")
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()]
    assert ids == ["DRAFTKINGS"]
