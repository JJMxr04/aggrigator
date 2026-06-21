"""ensure_team_logo: fetch, negative-cache, idempotence."""

from __future__ import annotations

import httpx
import pytest

from aggrigator.ingest.logos import ensure_team_logo
from aggrigator.ingest.odds_api_http import OddsApiHttpClient
from aggrigator.models import League, Sport, Team


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


@pytest.mark.asyncio
async def test_force_refetches_ok_row(session):
    team = await _team(session)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        body = b"V1" if calls["n"] == 1 else b"V2"
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    client = _client(handler)
    row = await ensure_team_logo(session, client, team)
    assert row.image == b"V1"
    # force=True must re-hit the API even though an ok row exists.
    row2 = await ensure_team_logo(session, client, team, force=True)
    assert row2.image == b"V2"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_fresh_miss_not_refetched(session):
    team = await _team(session)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, json={})

    client = _client(handler)
    await ensure_team_logo(session, client, team)   # writes a fresh missing row
    await ensure_team_logo(session, client, team)   # within cooldown -> no refetch
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_keyless_team_returns_none(session):
    # A team with no odds_api_io_key cannot be fetched.
    session.add(Sport(id="soccer", name="Soccer"))
    await session.flush()
    session.add(League(id="epl", sport_id="soccer", name="EPL"))
    await session.flush()
    team = Team(
        id="epl:_canon:foo", league_id="epl", team_id="_canon:foo",
        name_long="Foo", canonical_name="Foo", odds_api_io_key=None,
    )
    session.add(team)
    await session.flush()

    def handler(request):
        raise AssertionError("must not call odds-api for a keyless team")

    row = await ensure_team_logo(session, _client(handler), team)
    assert row is None
