"""Tests for /v1/events, /v1/events/{id}, /v1/events/{id}/markets — public."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tests.integration.factories import (
    make_event,
    make_league,
    make_market,
    make_selection,
    make_sport,
    make_team,
)

pytestmark = pytest.mark.asyncio


async def _seed_basic_event(session, **event_kwargs):
    sport = await make_sport(session, id="FOOTBALL", name="Football")
    league = await make_league(session, sport=sport, id="NFL", name="NFL")
    home = await make_team(session, league=league, team_id="DAL", name_long="Dallas")
    away = await make_team(session, league=league, team_id="PHI", name_long="Philly")
    event = await make_event(
        session, league=league, home_team=home, away_team=away, **event_kwargs
    )
    return sport, league, home, away, event


async def test_list_events_is_public(client) -> None:
    r = await client.get("/v1/events")
    assert r.status_code == 200


async def test_list_events_returns_seeded_in_default_window(client, session) -> None:
    _, _, _, _, event = await _seed_basic_event(session)
    r = await client.get("/v1/events")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == event.id
    assert body["items"][0]["home_team"]["name_long"] == "Dallas"


async def test_list_events_default_window_excludes_far_future(client, session) -> None:
    _, _, _, _, _ = await _seed_basic_event(
        session,
        start_time=datetime.now(tz=timezone.utc) + timedelta(days=10),
    )
    r = await client.get("/v1/events")
    assert r.json()["total"] == 0


async def test_list_events_filter_by_sport_and_league(client, session) -> None:
    fb = await make_sport(session, id="FOOTBALL", name="Football")
    bb = await make_sport(session, id="BASKETBALL", name="Basketball")
    nfl = await make_league(session, sport=fb, id="NFL", name="NFL")
    nba = await make_league(session, sport=bb, id="NBA", name="NBA")
    nfl_h = await make_team(session, league=nfl, team_id="DAL")
    nfl_a = await make_team(session, league=nfl, team_id="PHI", name_long="Philly")
    nba_h = await make_team(session, league=nba, team_id="LAL", name_long="Lakers")
    nba_a = await make_team(session, league=nba, team_id="BOS", name_long="Celtics")
    await make_event(session, league=nfl, home_team=nfl_h, away_team=nfl_a)
    await make_event(session, league=nba, home_team=nba_h, away_team=nba_a)

    r = await client.get("/v1/events?sport=FOOTBALL")
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["sport_id"] == "FOOTBALL"

    r = await client.get("/v1/events?league=NBA")
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["league_id"] == "NBA"


async def test_list_events_filter_live(client, session) -> None:
    sport = await make_sport(session, id="FOOTBALL", name="Football")
    league = await make_league(session, sport=sport, id="NFL", name="NFL")
    home = await make_team(session, league=league, team_id="DAL")
    away = await make_team(session, league=league, team_id="PHI", name_long="Philly")
    await make_event(
        session, league=league, home_team=home, away_team=away,
        id="evt-live", status_type="inprogress", is_live=True,
    )
    away2 = await make_team(session, league=league, team_id="NYG", name_long="Giants")
    await make_event(
        session, league=league, home_team=home, away_team=away2,
        id="evt-pre", status_type="notstarted",
    )

    r = await client.get("/v1/events?live=true")
    ids = [it["id"] for it in r.json()["items"]]
    assert ids == ["evt-live"]


async def test_list_events_pagination(client, session) -> None:
    sport = await make_sport(session, id="FOOTBALL", name="Football")
    league = await make_league(session, sport=sport, id="NFL", name="NFL")
    home = await make_team(session, league=league, team_id="DAL")
    for i in range(5):
        away = await make_team(
            session, league=league, team_id=f"TEAM{i}",
            name_long=f"Team {i}",
        )
        await make_event(
            session, league=league, home_team=home, away_team=away,
            id=f"evt-{i}",
            start_time=datetime.now(tz=timezone.utc) + timedelta(hours=i + 1),
        )
    r = await client.get("/v1/events?page_size=2&page=2")
    body = r.json()
    assert body["total"] == 5
    assert body["pages"] == 3
    assert body["page"] == 2
    assert len(body["items"]) == 2


async def test_list_events_include_markets(client, session) -> None:
    _, _, _, _, event = await _seed_basic_event(session)
    market = await make_market(session, event=event, type="NFL_POINTS_ML")
    await make_selection(session, market=market, type="HOME", decimal_odds=Decimal("1.50"))
    await make_selection(session, market=market, type="AWAY", decimal_odds=Decimal("2.50"))

    r = await client.get("/v1/events?include=markets")
    body = r.json()
    item = body["items"][0]
    assert len(item["markets"]) == 1
    assert len(item["markets"][0]["selections"]) == 2


async def test_get_event_404(client) -> None:
    r = await client.get("/v1/events/does-not-exist")
    assert r.status_code == 404


async def test_get_event_with_markets(client, session) -> None:
    _, _, _, _, event = await _seed_basic_event(session)
    market = await make_market(session, event=event, type="NFL_POINTS_ML")
    await make_selection(session, market=market, type="HOME", decimal_odds=Decimal("1.50"))

    r = await client.get(f"/v1/events/{event.id}?include=markets")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == event.id
    assert len(body["markets"]) == 1
    assert "odds_meta" in body


async def test_get_event_markets_filters(client, session) -> None:
    _, _, _, _, event = await _seed_basic_event(session)
    ml = await make_market(
        session, event=event, type="NFL_POINTS_ML", category="MONEYLINE",
        id=f"{event.id}-ml-ft",
    )
    total = await make_market(
        session, event=event, type="NFL_POINTS_OU", category="TOTAL",
        line=Decimal("47.5"), id=f"{event.id}-ou-ft-47_5",
    )
    await make_selection(session, market=ml, type="HOME", decimal_odds=Decimal("1.5"))
    await make_selection(session, market=total, type="OVER", decimal_odds=Decimal("1.91"))

    r = await client.get(f"/v1/events/{event.id}/markets?category=TOTAL")
    body = r.json()
    assert len(body["markets"]) == 1
    assert body["markets"][0]["category"] == "TOTAL"


async def test_get_event_markets_min_decimal_validation(client, session) -> None:
    _, _, _, _, event = await _seed_basic_event(session)
    r = await client.get(
        f"/v1/events/{event.id}/markets?min_decimal=not-a-number"
    )
    assert r.status_code == 400
