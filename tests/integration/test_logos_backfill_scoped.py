"""run_backfill_team_logos honors league_id / retry_missing / force."""

from __future__ import annotations

from unittest import mock

import pytest

from aggrigator.models import League, Sport, Team, TeamLogo
from aggrigator.workers.tasks import logos as logos_task


async def _seed_two_leagues(session):
    session.add(Sport(id="basketball", name="Basketball"))
    await session.flush()
    session.add(League(id="usa-nba", sport_id="basketball", name="NBA"))
    session.add(League(id="esp-acb", sport_id="basketball", name="ACB"))
    await session.flush()
    # NBA: one team with a stale 'missing' logo, one never-fetched.
    session.add(Team(id="usa-nba:38", league_id="usa-nba", team_id="38",
                     name_long="Lakers", canonical_name="Lakers", odds_api_io_key="38"))
    session.add(Team(id="usa-nba:39", league_id="usa-nba", team_id="39",
                     name_long="Celtics", canonical_name="Celtics", odds_api_io_key="39"))
    session.add(TeamLogo(team_id="usa-nba:38", image=None, content_type=None,
                         byte_size=0, etag=None, status="missing"))
    # ACB: one team, already 'ok'.
    session.add(Team(id="esp-acb:7", league_id="esp-acb", team_id="7",
                     name_long="Madrid", canonical_name="Madrid", odds_api_io_key="7"))
    session.add(TeamLogo(team_id="esp-acb:7", image=b"OK", content_type="image/png",
                         byte_size=2, etag="ok", status="ok"))
    await session.commit()


def _patches(calls):
    async def fake_ensure(sess, client, team, **kw):
        calls.append((team.id, kw.get("force", False)))
        row = await sess.get(TeamLogo, team.id)
        if row is None:
            row = TeamLogo(team_id=team.id, status="ok")
            sess.add(row)
        row.image, row.content_type, row.byte_size = b"X", "image/png", 1
        row.etag, row.status = "e", "ok"
        return row

    return (
        mock.patch.object(logos_task, "_build_client",
                          return_value=mock.Mock(close=mock.Mock())),
        mock.patch.object(logos_task, "quota_status",
                          return_value=mock.Mock(should_skip=False)),
        mock.patch.object(logos_task, "ensure_team_logo", side_effect=fake_ensure),
    )


@pytest.mark.asyncio
async def test_league_scope_limits_to_one_league(session):
    await _seed_two_leagues(session)
    calls = []
    p1, p2, p3 = _patches(calls)
    with p1, p2, p3:
        summary = await logos_task.run_backfill_team_logos(league_id="usa-nba")
    touched = {c[0] for c in calls}
    assert touched == {"usa-nba:39"}            # only the never-fetched NBA team
    assert "esp-acb:7" not in touched
    assert summary["league_id"] == "usa-nba"
    assert summary["fetched"] == 1
    assert summary["missing"] == 0


@pytest.mark.asyncio
async def test_retry_missing_includes_prior_404(session):
    await _seed_two_leagues(session)
    calls = []
    p1, p2, p3 = _patches(calls)
    with p1, p2, p3:
        await logos_task.run_backfill_team_logos(league_id="usa-nba", retry_missing=True)
    touched = {c[0] for c in calls}
    assert touched == {"usa-nba:38", "usa-nba:39"}   # the 'missing' row is retried
    # retried teams are fetched with force=True (bypass cooldown)
    assert any(cid == "usa-nba:38" and forced for cid, forced in calls)


@pytest.mark.asyncio
async def test_force_refetches_ok_logos(session):
    await _seed_two_leagues(session)
    calls = []
    p1, p2, p3 = _patches(calls)
    with p1, p2, p3:
        summary = await logos_task.run_backfill_team_logos(league_id="esp-acb", force=True)
    assert ("esp-acb:7", True) in calls          # the already-ok team is re-fetched
    assert summary["forced"] == 1


@pytest.mark.asyncio
async def test_default_is_unchanged_only_never_fetched(session):
    await _seed_two_leagues(session)
    calls = []
    p1, p2, p3 = _patches(calls)
    with p1, p2, p3:
        await logos_task.run_backfill_team_logos()   # nightly-cron defaults
    touched = {c[0] for c in calls}
    assert touched == {"usa-nba:39"}             # only the never-fetched team, all leagues
