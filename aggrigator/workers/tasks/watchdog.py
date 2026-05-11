"""Lifecycle watchdog cron — finds stuck events SGO never finalized.

Hourly cron that scans for events past ``AGG_LIFECYCLE_STALE_GRACE_HOURS``
still in ``notstarted`` / ``inprogress`` (informational, no DB writes).
If ``AGG_LIFECYCLE_AUTO_VOID_HOURS`` is set, events past that longer
threshold are presumptively VOIDED — see ``ingest/watchdog.py`` docstring
for the recovery story.
"""

from __future__ import annotations

import logging
from typing import Any

from aggrigator.config import get_settings
from aggrigator.db import session_scope
from aggrigator.ingest.watchdog import run_watchdog
from aggrigator.ops.progress import set_progress
from aggrigator.webhooks.notify import notify_webhook_worker

logger = logging.getLogger(__name__)


async def run_lifecycle_watchdog() -> dict[str, Any]:
    settings = get_settings()
    # Enable the disappearance branch only when on odds-api.io — SGO emits
    # explicit ``status.cancelled`` so its events never go missing without a
    # status flag. Leaving disappearance void_hours at 0 on SGO is a no-op.
    disappeared_void_hours = (
        settings.lifecycle_disappeared_void_hours
        if settings.odds_provider == "oddsapi"
        else 0
    )
    await set_progress(
        f"watchdog: scanning (stale>{settings.lifecycle_stale_grace_hours}h, "
        f"auto_void>{settings.lifecycle_auto_void_hours}h"
        + (" — DISABLED" if settings.lifecycle_auto_void_hours == 0 else "")
        + (f", disappeared>{disappeared_void_hours}h" if disappeared_void_hours > 0 else "")
        + ")"
    )
    async with session_scope() as session:
        report = await run_watchdog(
            session,
            stale_grace_hours=settings.lifecycle_stale_grace_hours,
            auto_void_hours=settings.lifecycle_auto_void_hours,
            disappeared_void_hours=disappeared_void_hours,
        )
    # session_scope commits on exit. Push-on-write: kick the webhook
    # worker if the watchdog inserted any voided-event deliveries.
    if report.deliveries_enqueued > 0:
        await notify_webhook_worker()

    await set_progress(
        f"watchdog: {report.stale_count} stale, {report.voided_count} voided, "
        f"{report.selections_voided} selections → VOID, "
        f"{report.deliveries_enqueued} webhook(s) enqueued"
    )
    summary = {
        "stale_count": report.stale_count,
        "stale_event_ids": report.stale_event_ids,
        "voided_count": report.voided_count,
        "voided_event_ids": report.voided_event_ids,
        "selections_voided": report.selections_voided,
        "deliveries_enqueued": report.deliveries_enqueued,
    }
    logger.info("lifecycle_watchdog: %s", summary)
    return summary


async def lifecycle_watchdog_task(ctx: dict) -> dict:
    """ARQ-callable wrapper. Wrapped by ``cron_run_recorder`` so each run
    lands in ``cron_run`` for /ops visibility."""
    from aggrigator.ops.recorder import cron_run_recorder

    @cron_run_recorder("lifecycle_watchdog")
    async def _runner(ctx_):
        return await run_lifecycle_watchdog()

    return await _runner(ctx)
