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


async def seed_sports(
    session: AsyncSession, client: SgoClient, *, _payloads: list[dict] | None = None,
) -> int:
    """Upsert every Sport SGO knows about. Returns count.

    ``_payloads`` lets a caller (``seed_all``) pass pre-fetched data so we
    don't make the same SGO call twice. Public callers ignore it.
    """
    payloads = _payloads if _payloads is not None else list(client.get_sports())
    count = 0
    for payload in payloads:
        if await upsert_sport_from_sgo(session, payload) is not None:
            count += 1
    await session.flush()
    logger.info("seeded %d sports", count)
    return count


async def seed_leagues(
    session: AsyncSession, client: SgoClient, *, _payloads: list[dict] | None = None,
) -> int:
    """Upsert every League SGO knows about, across every sport. Idempotent.

    SGO's ``/leagues`` endpoint returns every league in one response when
    no ``sportID`` filter is passed — so a single call replaces what used
    to be N calls (one per sport). Cuts seed runtime by an order of
    magnitude on a throttled client.
    """
    payloads = _payloads if _payloads is not None else list(client.get_leagues())
    count = 0
    for payload in payloads:
        if await upsert_league_from_sgo(session, payload) is not None:
            count += 1
    await session.flush()
    logger.info("seeded %d leagues", count)
    return count


async def seed_all(session: AsyncSession, client: SgoClient) -> dict:
    """Pre-fetch sports + leagues once and feed them into the upsert helpers
    so we never round-trip the same SGO endpoint twice in one seed run.

    Old shape: 1× /sports (in seed_sports) + 1× /sports (in seed_leagues) +
    N× /leagues?sportID=X — totalling 2 + N SGO calls.
    New shape: 1× /sports + 1× /leagues — total 2 SGO calls regardless
    of how many sports SGO has.
    """
    sport_payloads = list(client.get_sports())
    league_payloads = list(client.get_leagues())
    sports = await seed_sports(session, client, _payloads=sport_payloads)
    leagues = await seed_leagues(session, client, _payloads=league_payloads)
    return {"sports": sports, "leagues": leagues}
