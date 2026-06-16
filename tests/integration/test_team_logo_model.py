"""TeamLogo model round-trips bytes through Postgres."""

from __future__ import annotations

import pytest

from aggrigator.models import League, Sport, Team, TeamLogo


@pytest.mark.asyncio
async def test_team_logo_roundtrips_bytes(session):
    session.add(Sport(id="basketball", name="Basketball"))
    await session.flush()
    session.add(League(id="usa-nba", sport_id="basketball", name="NBA"))
    await session.flush()
    session.add(
        Team(
            id="usa-nba:38", league_id="usa-nba", team_id="38",
            name_long="Lakers", canonical_name="Lakers",
            odds_api_io_key="38",
        )
    )
    await session.flush()
    session.add(
        TeamLogo(
            team_id="usa-nba:38", image=b"\x89PNG\r\n",
            content_type="image/png", byte_size=6, etag="deadbeef",
            status="ok",
        )
    )
    await session.flush()

    row = await session.get(TeamLogo, "usa-nba:38")
    assert row.image == b"\x89PNG\r\n"
    assert row.status == "ok"
    assert row.content_type == "image/png"
