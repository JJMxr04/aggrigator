"""Sport / League seeders.

Two independent crons since sports change essentially never (the upstream
``/sports`` list is football, basketball, baseball, hockey, …) and leagues
shift more often (new minor leagues, season transitions). Independent
schedules → independent visibility in /ops/crons and tighter provider quota
budgets:

- ``seed_sports``  — weekly Mondays @ 01:30 UTC (1 provider call/week)
- ``seed_leagues`` — daily         @ 02:00 UTC (1 provider call/day)

``run_seed_sports_and_leagues`` is preserved as a composite helper —
``full_refresh`` calls it so the daily one-click refresh remains
self-sufficient (defensive seed before walking active leagues).
"""

from __future__ import annotations

import logging

from aggrigator.db import session_scope
from aggrigator.ingest.seed import seed_leagues, seed_sports, seed_all
from aggrigator.ops.recorder import cron_run_recorder
from aggrigator.workers.app import app
from aggrigator.workers.tasks.ingest import _build_client

logger = logging.getLogger(__name__)


async def run_seed_sports() -> dict:
    """Upsert every Sport row from the provider. One provider call (``/sports``)."""
    logger.info("seed_sports: starting (1 provider call expected) — fetching /sports")
    client = _build_client()
    async with session_scope() as session:
        sports = await seed_sports(session, client)
    result = {"sports": sports}
    logger.info("seed_sports: complete %s", result)
    return result


async def run_seed_leagues() -> dict:
    """Upsert every League row from the provider. One provider call (``/leagues``).

    Assumes ``seed_sports`` has already run — leagues reference sports
    via FK. On a cold DB the FK is ``ondelete=SET NULL`` so missing sports
    won't error, but the linkage will be incomplete until the next
    seed_sports run.
    """
    logger.info("seed_leagues: starting (1 provider call expected) — fetching /leagues")
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
    logger.info("seed_sports_and_leagues: starting (2 provider calls expected)")
    client = _build_client()
    async with session_scope() as session:
        result = await seed_all(session, client)
    logger.info("seed_sports_and_leagues: complete %s", result)
    return result


# ---- Procrastinate entry points -------------------------------------------


@app.periodic(cron="30 1 * * *")
@app.task(
    name="aggrigator.seed_sports",
    pass_context=True,
    queueing_lock="seed_sports",
)
@cron_run_recorder("seed_sports")
async def seed_sports_task(context, timestamp: int | None = None) -> dict:
    return await run_seed_sports()


@app.periodic(cron="0 2 * * *")
@app.task(
    name="aggrigator.seed_leagues",
    pass_context=True,
    queueing_lock="seed_leagues",
)
@cron_run_recorder("seed_leagues")
async def seed_leagues_task(context, timestamp: int | None = None) -> dict:
    return await run_seed_leagues()
