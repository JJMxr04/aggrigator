"""Nightly settlement backfill — picks up any selections the hot path missed."""

from __future__ import annotations

import logging

from aggrigator.db import session_scope
from aggrigator.ingest.settlement_computed import settle_pending_events

logger = logging.getLogger(__name__)


async def run_settle_pending(lookback_hours: int = 48) -> dict:
    async with session_scope() as session:
        report = await settle_pending_events(session, lookback_hours=lookback_hours)
    logger.info("settle_pending report: %s", report)
    return report


async def settle_pending_task(ctx: dict) -> dict:
    """ARQ-callable wrapper. Records each scheduled run in ``cron_run``."""
    from aggrigator.ops.recorder import cron_run_recorder

    @cron_run_recorder("settle_pending")
    async def _runner(ctx_):
        return await run_settle_pending()

    return await _runner(ctx)
