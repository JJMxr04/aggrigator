"""Integration tests for GET /v1/teams (keyed, paginated, trimmed)."""

from __future__ import annotations

import pytest

from tests.integration.factories import make_league, make_sport, make_team, make_tenant_user

pytestmark = pytest.mark.asyncio


async def _seed_teams(session, n=5, league_id="NFL"):
    sport = await make_sport(session, id="FOOTBALL", name="Football")
    league = await make_league(session, sport=sport, id=league_id, name=league_id)
    teams = []
    for i in range(n):
        t = await make_team(
            session, league=league, team_id=f"T{i:02d}",
            name_long=f"Team {i:02d}", name_medium=f"Tm{i:02d}", name_short=f"T{i}",
        )
        teams.append(t)
    return sport, league, teams


async def test_list_teams_paginates(client, session):
    await _seed_teams(session, n=5)
    r = await client.get("/v1/teams", params={"page": 1, "page_size": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    assert body["pages"] == 3
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert len(body["items"]) == 2
    r2 = await client.get("/v1/teams", params={"page": 2, "page_size": 2})
    ids_p1 = {it["id"] for it in body["items"]}
    ids_p2 = {it["id"] for it in r2.json()["items"]}
    assert ids_p1.isdisjoint(ids_p2)


async def test_list_teams_filters_by_league(client, session):
    sport, _, _ = await _seed_teams(session, n=3, league_id="NFL")
    other = await make_league(session, sport=sport, id="MLB", name="MLB")
    await make_team(session, league=other, team_id="NYY", name_long="Yankees")
    r = await client.get("/v1/teams", params={"league_id": "MLB"})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["league_id"] == "MLB"


async def test_list_teams_item_excludes_internal_fields(client, session):
    await _seed_teams(session, n=1)
    body = (await client.get("/v1/teams")).json()
    item = body["items"][0]
    assert "odds_api_io_key" not in item
    assert "thesportsdb_team_id" not in item
    assert "public_id" not in item
    assert "logo_url" not in item
    # Exact set, not superset: any field that leaks onto the wire fails here.
    assert set(item) == {
        "id", "league_id", "team_id", "sport_id", "name_long",
        "name_medium", "name_short", "primary_color", "secondary_color",
        "primary_contrast", "secondary_contrast", "stat_entity_id",
    }


async def test_list_teams_bad_key_401(client, session):
    await _seed_teams(session, n=1)
    r = await client.get(
        "/v1/teams", headers={"X-Aggrigator-Tenant-Key": "agg_test_bogusbogus"},
    )
    assert r.status_code == 401


async def test_list_teams_valid_key_passes(client, session):
    await _seed_teams(session, n=1)
    _, raw = await make_tenant_user(session, email="teamreader@example.com")
    r = await client.get("/v1/teams", headers={"X-Aggrigator-Tenant-Key": raw})
    assert r.status_code == 200
    assert r.json()["total"] == 1
