"""Nightly settlement backfill — picks up any selections the hot path missed."""

from __future__ import annotations

import logging

from aggrigator.db import session_scope
from aggrigator.ingest.settlement_computed import settle_pending_events
from aggrigator.ops.recorder import cron_run_recorder
from aggrigator.workers.app import app

logger = logging.getLogger(__name__)


async def run_settle_pending(lookback_hours: int = 48) -> dict:
    async with session_scope() as session:
        report = await settle_pending_events(session, lookback_hours=lookback_hours)
    logger.info("settle_pending report: %s", report)
    return report


@app.periodic(cron="30 3 * * *")
@app.task(
    name="aggrigator.settle_pending",
    pass_context=True,
    queueing_lock="settle_pending",
)
@cron_run_recorder("settle_pending")
async def settle_pending_task(context, timestamp: int | None = None) -> dict:
    return await run_settle_pending()
