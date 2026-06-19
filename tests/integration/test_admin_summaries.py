"""Aggregate summary queries group rows by league correctly."""

from __future__ import annotations

import pytest

from aggrigator.admin.summaries import events_by_league, teams_by_league
from tests.integration.factories import (
    make_event,
    make_league,
    make_sport,
    make_team,
)

pytestmark = pytest.mark.asyncio


async def _seed(session):
    sport = await make_sport(session, id="soccer", name="Soccer")
    epl = await make_league(session, sport=sport, id="epl", name="EPL")
    laliga = await make_league(session, sport=sport, id="laliga", name="La Liga")
    # EPL: 2 teams, 1 confirmed; La Liga: 1 team, 0 confirmed.
    t1 = await make_team(session, league=epl, team_id="ARS")
    t2 = await make_team(session, league=epl, team_id="CHE")
    t1.match_confirmed = True
    session.add(t1)
    await session.commit()
    t3 = await make_team(session, league=laliga, team_id="RMA")
    # EPL: 2 finished + 1 notstarted; La Liga: 1 notstarted.
    await make_event(session, league=epl, home_team=t1, away_team=t2, status_type="finished")
    await make_event(session, league=epl, home_team=t2, away_team=t1, status_type="finished")
    await make_event(session, league=epl, home_team=t1, away_team=t2, status_type="notstarted")
    await make_event(session, league=laliga, home_team=t3, away_team=t3, status_type="notstarted")
    return epl, laliga


async def test_events_by_league_counts(session):
    await _seed(session)
    rows = await events_by_league(session)
    by_name = {r["league"]: r for r in rows}
    assert by_name["EPL"]["total"] == 3
    assert by_name["EPL"]["by_status"]["finished"] == 2
    assert by_name["EPL"]["by_status"]["notstarted"] == 1
    assert by_name["La Liga"]["total"] == 1
    # Sorted by total desc → EPL first.
    assert rows[0]["league"] == "EPL"


async def test_teams_by_league_confirmed_split(session):
    await _seed(session)
    rows = await teams_by_league(session)
    by_name = {r["league"]: r for r in rows}
    assert by_name["EPL"]["total"] == 2
    assert by_name["EPL"]["confirmed"] == 1
    assert by_name["EPL"]["unconfirmed"] == 1
    assert by_name["La Liga"]["total"] == 1
    assert by_name["La Liga"]["confirmed"] == 0
