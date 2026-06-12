"""Tests for the enriched read-endpoint shape (sport/league nested,
team.name alias, computed odds envelopes, market.subject_team)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.integration.factories import (
    make_bookmaker,
    make_event,
    make_league,
    make_market,
    make_selection,
    make_sport,
    make_team,
)

pytestmark = pytest.mark.asyncio


async def _seed_full_chain(session, *, with_market: bool = False):
    sport = await make_sport(session, id="FOOTBALL", name="Football", short_name="FB")
    league = await make_league(session, sport=sport, id="NFL", name="NFL", short_name="NFL")
    home = await make_team(session, league=league, team_id="DAL", name_long="Dallas Cowboys")
    away = await make_team(session, league=league, team_id="PHI", name_long="Philadelphia Eagles")
    event = await make_event(session, league=league, home_team=home, away_team=away)
    if with_market:
        # /v1/events lists only events with markets (Event.markets.any());
        # distinct id so tests that count their own markets aren't polluted.
        await make_market(session, event=event, id=f"{event.id}-seed-ml")
    return sport, league, home, away, event


async def test_event_list_includes_nested_sport_and_league(client, session) -> None:
    sport, league, *_ = await _seed_full_chain(session, with_market=True)
    r = await client.get("/v1/events")
    assert r.status_code == 200
    item = r.json()["items"][0]
    # IDs still present (backwards-compat for ID-based consumers).
    assert item["sport_id"] == "FOOTBALL"
    assert item["league_id"] == "NFL"
    # Nested objects too.
    assert item["sport"]["id"] == "FOOTBALL"
    assert item["sport"]["name"] == "Football"
    assert item["league"]["id"] == "NFL"
    assert item["league"]["name"] == "NFL"


async def test_team_includes_name_alias(client, session) -> None:
    await _seed_full_chain(session, with_market=True)
    r = await client.get("/v1/events")
    item = r.json()["items"][0]
    # ``name`` is the legacy-template-friendly alias of ``name_long``.
    assert item["home_team"]["name"] == "Dallas Cowboys"
    assert item["home_team"]["name_long"] == "Dallas Cowboys"
    assert item["away_team"]["name"] == "Philadelphia Eagles"


async def test_selection_includes_odds_envelope(client, session) -> None:
    sport, league, home, away, event = await _seed_full_chain(session)
    market = await make_market(session, event=event, type="NFL_POINTS_ML")
    # Use clean conversion values: 2.50 → +150, 1.50 → -200.
    await make_selection(
        session, market=market, type="HOME", decimal_odds=Decimal("2.5000"),
        opening_decimal_odds=Decimal("1.5000"),
    )

    r = await client.get(f"/v1/events/{event.id}/markets")
    selection = r.json()["markets"][0]["selections"][0]
    # Both the raw decimal and the computed envelope are present.
    assert selection["decimal_odds"] == "2.5000"
    assert selection["odds"]["decimal"] == "2.5000"
    assert selection["odds"]["american"] == 150  # 2.5 → +150
    assert selection["odds"]["fractional"] == "3/2"
    # Opening odds exist with the same envelope shape.
    assert selection["opening_odds"]["american"] == -200  # 1.5 → -200
    assert selection["opening_odds"]["decimal"] == "1.5000"


async def test_selection_odds_is_null_when_decimal_odds_is_null(client, session) -> None:
    sport, league, home, away, event = await _seed_full_chain(session)
    market = await make_market(session, event=event, type="NFL_POINTS_ML")
    # Suspended selection — decimal_odds is None.
    await make_selection(session, market=market, type="HOME", decimal_odds=None)

    r = await client.get(f"/v1/events/{event.id}/markets")
    selection = r.json()["markets"][0]["selections"][0]
    assert selection["decimal_odds"] is None
    assert selection["odds"] is None
    assert selection["opening_odds"] is None


async def test_market_includes_nested_subject_team(client, session) -> None:
    sport, league, home, away, event = await _seed_full_chain(session)
    # Per-team total — subject_team_id points at home.
    market = await make_market(
        session, event=event, type="NFL_POINTS_OU_HOME",
        category="PROPS_TEAM",
    )
    market.subject_team_id = home.id
    await session.commit()
    await make_selection(session, market=market, type="OVER", decimal_odds=Decimal("1.91"))

    r = await client.get(f"/v1/events/{event.id}/markets")
    market_dto = r.json()["markets"][0]
    # ID + nested object both present.
    assert market_dto["subject_team_id"] == home.id
    assert market_dto["subject_team"]["id"] == home.id
    assert market_dto["subject_team"]["name"] == "Dallas Cowboys"


async def test_event_detail_with_markets_carries_nested_objects(client, session) -> None:
    sport, league, home, away, event = await _seed_full_chain(session)
    market = await make_market(session, event=event, type="NFL_POINTS_ML")
    await make_selection(session, market=market, type="HOME", decimal_odds=Decimal("1.5"))

    r = await client.get(f"/v1/events/{event.id}?include=markets")
    body = r.json()
    assert body["sport"]["name"] == "Football"
    assert body["league"]["name"] == "NFL"
    assert body["home_team"]["name"] == "Dallas Cowboys"
    assert body["markets"][0]["selections"][0]["odds"]["decimal"] == "1.5000"
