"""Lifecycle watchdog cron — finds stuck events the upstream never finalized.

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
from aggrigator.ops.recorder import cron_run_recorder
from aggrigator.webhooks.notify import notify_webhook_worker
from aggrigator.workers.app import app

logger = logging.getLogger(__name__)


async def run_lifecycle_watchdog() -> dict[str, Any]:
    settings = get_settings()
    disappeared_void_hours = settings.lifecycle_disappeared_void_hours
    logger.info(
        "watchdog: scanning (stale>%dh, auto_void>%dh%s%s)",
        settings.lifecycle_stale_grace_hours,
        settings.lifecycle_auto_void_hours,
        " — DISABLED" if settings.lifecycle_auto_void_hours == 0 else "",
        f", disappeared>{disappeared_void_hours}h" if disappeared_void_hours > 0 else "",
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


# Hourly @ :45 — keeps it clear of the refresh tick at :00.
@app.periodic(cron="45 * * * *")
@app.task(
    name="aggrigator.lifecycle_watchdog",
    pass_context=True,
    queueing_lock="lifecycle_watchdog",
)
@cron_run_recorder("lifecycle_watchdog")
async def lifecycle_watchdog_task(context, timestamp: int | None = None) -> dict:
    return await run_lifecycle_watchdog()
