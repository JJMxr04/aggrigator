"""Logo backfill — fetch missing team crests league by league.

``run_backfill_team_logos`` is a plain async fn (tests call it directly).
``backfill_team_logos_task`` is the Procrastinate periodic wrapper. Per-league
iteration paces the odds-api budget; the quota gate hard-stops when the
per-hour bucket is near its cap.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import or_, select

from aggrigator.config import get_settings
from aggrigator.db import session_scope
from aggrigator.ingest.logos import ensure_team_logo
from aggrigator.ingest.odds_api_http import OddsApiHttpClient
from aggrigator.ingest.odds_api_quota import quota_status
from aggrigator.models import Team, TeamLogo
from aggrigator.workers.app import app

logger = logging.getLogger(__name__)


def _build_client() -> OddsApiHttpClient:
    settings = get_settings()
    return OddsApiHttpClient(
        api_key=settings.odds_api_key,
        max_retries=settings.odds_api_max_retries,
    )


def _logo_target_query(league_id: str | None, retry_missing: bool, force: bool):
    """Teams to (re)fetch, by policy. Only teams with a provider key qualify."""
    stmt = select(Team).where(Team.odds_api_io_key.is_not(None))
    if league_id is not None:
        stmt = stmt.where(Team.league_id == league_id)
    if force:                       # every keyed team in scope
        return stmt.order_by(Team.league_id, Team.id)
    stmt = stmt.outerjoin(TeamLogo, TeamLogo.team_id == Team.id)
    if retry_missing:               # no row, or a row that isn't 'ok'
        stmt = stmt.where(or_(TeamLogo.team_id.is_(None), TeamLogo.status != "ok"))
    else:                           # default: only never-fetched
        stmt = stmt.where(TeamLogo.team_id.is_(None))
    return stmt.order_by(Team.league_id, Team.id)


async def run_backfill_team_logos(
    *,
    league_id: str | None = None,
    retry_missing: bool = False,
    force: bool = False,
    max_per_run: int = 500,
) -> dict[str, Any]:
    settings = get_settings()
    client = _build_client()
    qs = quota_status(client, threshold_pct=settings.odds_api_throttle_pct)
    if not settings.test_mode and qs.should_skip:
        client.close()
        logger.warning("backfill_team_logos: SKIPPED — quota guard (%s)", qs.pace_reason)
        return {"skipped": True, "reason": "oddsapi_per_hour_quota"}

    fetched = 0
    missing = 0
    forced = 0
    try:
        async with session_scope() as session:
            # team_ids that already hold an 'ok' logo — for the `forced` count.
            # forced count is only meaningful on the force path; ok_ids stays empty otherwise
            ok_ids: set[str] = set()
            if force:
                ok_ids = set(
                    (await session.scalars(
                        select(TeamLogo.team_id).where(TeamLogo.status == "ok")
                    )).all()
                )
            teams = list(
                (await session.scalars(
                    _logo_target_query(league_id, retry_missing, force)
                )).all()
            )
            for team in teams:
                if fetched + missing >= max_per_run:
                    break
                row = await ensure_team_logo(
                    session, client, team, force=force or retry_missing
                )
                if row is not None and row.status == "ok":
                    fetched += 1
                    if team.id in ok_ids:
                        forced += 1
                else:
                    missing += 1
    finally:
        client.close()

    summary = {
        "league_id": league_id,
        "fetched": fetched,
        "missing": missing,
        "forced": forced,
    }
    logger.info("backfill_team_logos: %s", summary)
    return summary


@app.periodic(cron="15 4 * * *")
@app.task(
    name="aggrigator.backfill_team_logos",
    pass_context=True,
    queueing_lock="backfill_team_logos",
)
async def backfill_team_logos_task(context, timestamp: int | None = None) -> dict:
    return await run_backfill_team_logos()


async def run_fetch_team_logo(team_pk: str) -> dict[str, Any]:
    """Fetch one team's logo. Used by the on-create hook (next task)."""
    client = _build_client()
    try:
        async with session_scope() as session:
            team = await session.get(Team, team_pk)
            if team is None:
                return {"team": team_pk, "result": "team_not_found"}
            row = await ensure_team_logo(session, client, team)
            return {
                "team": team_pk,
                "result": row.status if row is not None else "no_key",
            }
    finally:
        client.close()


@app.task(name="aggrigator.fetch_team_logo")
async def fetch_team_logo_task(team_pk: str) -> dict:
    return await run_fetch_team_logo(team_pk)
