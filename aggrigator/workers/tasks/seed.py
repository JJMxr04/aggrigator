"""Sport / League seeders.

Two independent crons since sports change essentially never (the SGO
``/sports`` list is football, basketball, baseball, hockey, …) and leagues
shift more often (new minor leagues, season transitions). Independent
schedules → independent visibility in /ops/crons and tighter SGO quota
budgets:

- ``seed_sports``  — weekly Mondays @ 01:30 UTC (1 SGO call/week)
- ``seed_leagues`` — daily         @ 02:00 UTC (1 SGO call/day)

``run_seed_sports_and_leagues`` is preserved as a composite helper —
``full_refresh`` calls it so the daily one-click refresh remains
self-sufficient (defensive seed before walking active leagues).
"""

from __future__ import annotations

import logging

from aggrigator.db import session_scope
from aggrigator.ingest.seed import seed_leagues, seed_sports, seed_all
from aggrigator.ops.progress import set_progress
from aggrigator.workers.tasks.ingest import _build_client

logger = logging.getLogger(__name__)


async def run_seed_sports() -> dict:
    """Upsert every Sport row from SGO. One SGO call (``/sports``)."""
    logger.info("seed_sports: starting (1 SGO call expected)")
    await set_progress("fetching /sports from SGO")
    client = _build_client()
    async with session_scope() as session:
        sports = await seed_sports(session, client)
    result = {"sports": sports}
    logger.info("seed_sports: complete %s", result)
    return result


async def run_seed_leagues() -> dict:
    """Upsert every League row from SGO. One SGO call (``/leagues``).

    Assumes ``seed_sports`` has already run — leagues reference sports
    via FK. On a cold DB the FK is ``ondelete=SET NULL`` so missing sports
    won't error, but the linkage will be incomplete until the next
    seed_sports run.
    """
    logger.info("seed_leagues: starting (1 SGO call expected)")
    await set_progress("fetching /leagues from SGO")
    client = _build_client()
    async with session_scope() as session:
        leagues = await seed_leagues(session, client)
    result = {"leagues": leagues}
    logger.info("seed_leagues: complete %s", result)
    return result


async def run_seed_sports_and_leagues() -> dict:
    """Both halves in one call — used by ``full_refresh`` as a defensive
    pre-step so the daily refresh is self-sufficient even if the standalone
    seed crons haven't fired yet."""
    logger.info("seed_sports_and_leagues: starting (2 SGO calls expected)")
    client = _build_client()
    async with session_scope() as session:
        result = await seed_all(session, client)
    logger.info("seed_sports_and_leagues: complete %s", result)
    return result


# ---- ARQ entry points ------------------------------------------------------


async def seed_sports_task(ctx: dict) -> dict:
    from aggrigator.ops.recorder import cron_run_recorder

    @cron_run_recorder("seed_sports")
    async def _runner(ctx_):
        return await run_seed_sports()

    return await _runner(ctx)


async def seed_leagues_task(ctx: dict) -> dict:
    from aggrigator.ops.recorder import cron_run_recorder

    @cron_run_recorder("seed_leagues")
    async def _runner(ctx_):
        return await run_seed_leagues()

    return await _runner(ctx)
