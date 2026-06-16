"""ensure_team_logo: fetch, negative-cache, idempotence."""

from __future__ import annotations

import httpx
import pytest

from aggrigator.ingest.logos import ensure_team_logo
from aggrigator.ingest.odds_api_http import OddsApiHttpClient
from aggrigator.models import League, Sport, Team, TeamLogo


def _client(handler) -> OddsApiHttpClient:
    c = OddsApiHttpClient(base_url="https://api.odds-api.io/v3", api_key="K", max_retries=0)
    c.client.close()
    c.client = httpx.Client(
        base_url="https://api.odds-api.io/v3", transport=httpx.MockTransport(handler)
    )
    return c


async def _team(session) -> Team:
    session.add(Sport(id="basketball", name="Basketball"))
    await session.flush()
    session.add(League(id="usa-nba", sport_id="basketball", name="NBA"))
    await session.flush()
    t = Team(
        id="usa-nba:38", league_id="usa-nba", team_id="38",
        name_long="Lakers", canonical_name="Lakers", odds_api_io_key="38",
    )
    session.add(t)
    await session.flush()
    return t


@pytest.mark.asyncio
async def test_ensure_fetches_and_stores(session):
    team = await _team(session)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, content=b"PNGDATA", headers={"content-type": "image/png"})

    client = _client(handler)
    row = await ensure_team_logo(session, client, team)
    assert row.status == "ok"
    assert row.image == b"PNGDATA"
    assert calls["n"] == 1

    # Second call: row is already "ok" — must not call the API again.
    row2 = await ensure_team_logo(session, client, team)
    assert row2.status == "ok"
    assert calls["n"] == 1  # no additional HTTP call


@pytest.mark.asyncio
async def test_ensure_negative_caches_404(session):
    team = await _team(session)

    def handler(request):
        return httpx.Response(404, json={})

    row = await ensure_team_logo(session, _client(handler), team)
    assert row.status == "missing"
    assert row.image is None
