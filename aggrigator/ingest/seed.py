"""Seed Sport / League rows from SGO.

The event-ingest cron (``ingest_due_leagues``) walks every active League. The
League rows have to exist first — that's what this module is for. Run it once
per deploy and again whenever SGO adds a sport / league.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.ingest.client import SgoClient
from aggrigator.ingest.upserts import upsert_league_from_sgo, upsert_sport_from_sgo

logger = logging.getLogger(__name__)


async def seed_sports(session: AsyncSession, client: SgoClient) -> int:
    """Upsert every Sport SGO knows about. Returns count."""
    count = 0
    for payload in client.get_sports():
        if await upsert_sport_from_sgo(session, payload) is not None:
            count += 1
    await session.flush()
    logger.info("seeded %d sports", count)
    return count


async def seed_leagues(session: AsyncSession, client: SgoClient) -> int:
    """Upsert every League SGO knows about, across every sport. Idempotent."""
    count = 0
    sports = client.get_sports()
    for sport in sports:
        sport_id = sport.get("sportID")
        if not sport_id:
            continue
        for payload in client.get_leagues(sport_id=sport_id):
            if await upsert_league_from_sgo(session, payload) is not None:
                count += 1
    await session.flush()
    logger.info("seeded %d leagues", count)
    return count


async def seed_all(session: AsyncSession, client: SgoClient) -> dict:
    sports = await seed_sports(session, client)
    leagues = await seed_leagues(session, client)
    return {"sports": sports, "leagues": leagues}
