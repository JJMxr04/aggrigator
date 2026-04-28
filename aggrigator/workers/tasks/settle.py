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
    return await run_settle_pending()
