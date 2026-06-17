"""GET /v1/teams/{id}/logo: hit, 304, 404."""

from __future__ import annotations

import httpx
import pytest

from aggrigator.models import League, Sport, Team, TeamLogo


async def _seed_team(session, *, with_logo: bool):
    session.add(Sport(id="basketball", name="Basketball"))
    await session.flush()
    session.add(League(id="usa-nba", sport_id="basketball", name="NBA"))
    await session.flush()
    session.add(
        Team(
            id="usa-nba:38", league_id="usa-nba", team_id="38",
            name_long="Lakers", canonical_name="Lakers", odds_api_io_key="38",
        )
    )
    await session.flush()
    if with_logo:
        session.add(
            TeamLogo(
                team_id="usa-nba:38", image=b"PNGDATA",
                content_type="image/png", byte_size=7, etag="abc123",
                status="ok",
            )
        )
    await session.commit()


@pytest.mark.asyncio
async def test_logo_hit(client, session):
    await _seed_team(session, with_logo=True)
    resp = await client.get("/v1/teams/usa-nba:38/logo")
    assert resp.status_code == 200
    assert resp.content == b"PNGDATA"
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["etag"] == '"abc123"'


@pytest.mark.asyncio
async def test_logo_304_on_matching_etag(client, session):
    await _seed_team(session, with_logo=True)
    resp = await client.get(
        "/v1/teams/usa-nba:38/logo", headers={"If-None-Match": '"abc123"'}
    )
    assert resp.status_code == 304


@pytest.mark.asyncio
async def test_logo_404_for_unknown_team(client, session):
    await _seed_team(session, with_logo=False)
    resp = await client.get("/v1/teams/usa-nba:999/logo")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_logo_503_on_oddsapi_failure_not_500_or_missing(
    client, session, monkeypatch
):
    """A transient odds-api failure on the lazy-fetch (e.g. odds-api 429)
    must surface 503 + Retry-After — NOT a 500, and NOT a poisoned 30-day
    'missing' row. The team exists with a provider key but has no cached
    crest, so the endpoint takes the lazy-fetch path."""
    await _seed_team(session, with_logo=False)

    req = httpx.Request("GET", "https://odds.example/participants/38/logo")
    resp_429 = httpx.Response(429, headers={"Retry-After": "12"}, request=req)

    async def _boom(*_args, **_kwargs):
        raise httpx.HTTPStatusError("rate limited", request=req, response=resp_429)

    monkeypatch.setattr("aggrigator.api.logos.ensure_team_logo", _boom)

    resp = await client.get("/v1/teams/usa-nba:38/logo")
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "12"
    # The lazy-fetch failure must not have written a negative-cache row.
    assert await session.get(TeamLogo, "usa-nba:38") is None
