"""Sport/League seeder — runs once at deploy and as a manual task."""

from __future__ import annotations

import logging

from aggrigator.db import session_scope
from aggrigator.ingest.seed import seed_all
from aggrigator.workers.tasks.ingest import _build_client

logger = logging.getLogger(__name__)


async def run_seed_sports_and_leagues() -> dict:
    client = _build_client()
    async with session_scope() as session:
        result = await seed_all(session, client)
    logger.info("seed_sports_and_leagues: %s", result)
    return result


async def seed_task(ctx: dict) -> dict:
    """ARQ-callable wrapper. Records each scheduled run in ``cron_run``."""
    from aggrigator.ops.recorder import cron_run_recorder

    @cron_run_recorder("seed_sports_and_leagues")
    async def _runner(ctx_):
        return await run_seed_sports_and_leagues()

    return await _runner(ctx)
