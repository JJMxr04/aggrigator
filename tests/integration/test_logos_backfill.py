"""run_backfill_team_logos fills teams that have no logo row yet."""

from __future__ import annotations

from unittest import mock

import pytest
from sqlalchemy import select

from aggrigator.models import League, Sport, Team, TeamLogo
from aggrigator.workers.tasks import logos as logos_task


@pytest.mark.asyncio
async def test_backfill_fills_missing(session):
    # Commit seed data so session_scope's independent session sees it.
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
    await session.commit()

    async def fake_ensure(sess, client, team, **kw):
        row = TeamLogo(
            team_id=team.id, image=b"X", content_type="image/png",
            byte_size=1, etag="e", status="ok",
        )
        sess.add(row)
        return row

    with mock.patch.object(logos_task, "_build_client",
                           return_value=mock.Mock(close=mock.Mock())), \
         mock.patch.object(logos_task, "quota_status",
                           return_value=mock.Mock(should_skip=False)), \
         mock.patch.object(logos_task, "ensure_team_logo", side_effect=fake_ensure):
        summary = await logos_task.run_backfill_team_logos()

    assert summary["fetched"] == 1
    assert summary["missing"] == 0

    # session_scope() committed its own transaction — roll back the test
    # session's open transaction so the connection re-reads committed state.
    await session.rollback()
    rows = (await session.scalars(select(TeamLogo))).all()
    assert any(r.status == "ok" for r in rows)


@pytest.mark.asyncio
async def test_backfill_skips_team_with_existing_logo(session):
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
    # Pre-existing logo row — backfill must skip this team.
    session.add(
        TeamLogo(
            team_id="usa-nba:38", image=b"OLD", content_type="image/png",
            byte_size=3, etag="old", status="ok",
        )
    )
    await session.commit()

    ensure_calls = []

    async def fake_ensure(sess, client, team, **kw):
        ensure_calls.append(team.id)
        row = TeamLogo(team_id=team.id, image=b"NEW", content_type="image/png",
                       byte_size=3, etag="new", status="ok")
        sess.add(row)
        return row

    with mock.patch.object(logos_task, "_build_client",
                           return_value=mock.Mock(close=mock.Mock())), \
         mock.patch.object(logos_task, "quota_status",
                           return_value=mock.Mock(should_skip=False)), \
         mock.patch.object(logos_task, "ensure_team_logo", side_effect=fake_ensure):
        summary = await logos_task.run_backfill_team_logos()

    # Team already had a logo — ensure_team_logo must not be called.
    assert ensure_calls == []
    assert summary["fetched"] == 0
    assert summary["missing"] == 0


@pytest.mark.asyncio
async def test_backfill_skips_keyless_team(session):
    session.add(Sport(id="basketball", name="Basketball"))
    await session.flush()
    session.add(League(id="usa-nba", sport_id="basketball", name="NBA"))
    await session.flush()
    session.add(
        Team(
            id="usa-nba:no-key", league_id="usa-nba", team_id="no-key",
            name_long="Anon", canonical_name="Anon", odds_api_io_key=None,
        )
    )
    await session.commit()

    ensure_calls = []

    async def fake_ensure(sess, client, team, **kw):
        ensure_calls.append(team.id)
        return None

    with mock.patch.object(logos_task, "_build_client",
                           return_value=mock.Mock(close=mock.Mock())), \
         mock.patch.object(logos_task, "quota_status",
                           return_value=mock.Mock(should_skip=False)), \
         mock.patch.object(logos_task, "ensure_team_logo", side_effect=fake_ensure):
        summary = await logos_task.run_backfill_team_logos()

    # WHERE clause filters out keyless teams — ensure_team_logo never called.
    assert ensure_calls == []
    assert summary["fetched"] == 0
    assert summary["missing"] == 0


@pytest.mark.asyncio
async def test_backfill_quota_skip(session):
    from aggrigator.config import get_settings
    real_settings = get_settings()
    # Build a fake settings object with test_mode=False so the quota guard fires.
    fake_settings = mock.Mock(
        wraps=real_settings,
        test_mode=False,
        odds_api_throttle_pct=real_settings.odds_api_throttle_pct,
    )
    with mock.patch.object(logos_task, "_build_client",
                           return_value=mock.Mock(close=mock.Mock())), \
         mock.patch.object(logos_task, "quota_status",
                           return_value=mock.Mock(should_skip=True)), \
         mock.patch("aggrigator.workers.tasks.logos.get_settings",
                    return_value=fake_settings):
        summary = await logos_task.run_backfill_team_logos()

    assert summary.get("skipped") is True
    assert summary.get("reason") == "oddsapi_per_hour_quota"
