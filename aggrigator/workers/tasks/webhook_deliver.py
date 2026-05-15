"""Webhook delivery worker — drains due rows, calls send_one, commits.

Triggered by push, NOT a polling cron:
- ``webhooks.notify.notify_webhook_worker`` is called from the ingest
  orchestrator + watchdog right after they commit new delivery rows.
  arq runs this task within milliseconds.
- Failed rows re-enqueue themselves via
  ``webhooks.notify.notify_webhook_retry`` with ``_defer_until=
  next_retry_at``, so retries pop out of arq's delayed-job queue at
  the right moment instead of being polled for.

Target + signing secret are read from settings (single MDProject receiver).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from arq.connections import ArqRedis
from sqlalchemy import or_, select

from aggrigator.config import get_settings
from aggrigator.db import async_session_factory
from aggrigator.models import WebhookDelivery
from aggrigator.webhooks.deliver import send_one
from aggrigator.webhooks.notify import notify_webhook_retry

logger = logging.getLogger(__name__)

BATCH_SIZE = 50


async def run_deliver_due(
    now: datetime | None = None,
    *,
    redis: ArqRedis | None = None,
) -> dict[str, int]:
    """Send everything that's due. Returns summary counts.

    ``redis`` is the arq pool from the calling task's ``ctx`` (or
    ``None`` for direct/test invocations). When set, transient failures
    re-enqueue themselves via ``notify_webhook_retry`` with
    ``_defer_until=next_retry_at`` so we don't need a polling cron.
    """
    settings = get_settings()
    target_url = settings.webhook_target_url
    secret = settings.webhook_secret

    if not target_url or not secret:
        logger.warning(
            "webhook_deliver: AGG_WEBHOOK_TARGET_URL / AGG_WEBHOOK_SECRET "
            "unset — skipping run. Rows remain in webhook_delivery for the "
            "next dispatch once config is set."
        )
        return {"sent": 0, "retried": 0, "failed": 0}

    moment = now or datetime.now(tz=timezone.utc)
    sent = 0
    retried = 0
    failed = 0

    async with async_session_factory() as session:
        rows = list(await session.scalars(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.delivered_at.is_(None),
                or_(
                    WebhookDelivery.next_retry_at.is_(None),
                    WebhookDelivery.next_retry_at <= moment,
                ),
            )
            .order_by(WebhookDelivery.next_retry_at.asc().nullsfirst())
            .limit(BATCH_SIZE)
        ))

        if not rows:
            return {"sent": 0, "retried": 0, "failed": 0}

        async with httpx.AsyncClient() as http:
            for row in rows:
                outcome = await send_one(
                    row, url=target_url, secret=secret, client=http,
                )
                # Commit unconditionally — even retry-decision paths mutate
                # last_error / next_retry_at and we want that persisted.
                await session.commit()
                if outcome.success:
                    sent += 1
                elif outcome.permanent_fail:
                    failed += 1
                else:
                    retried += 1
                    # Schedule a deferred re-run at the row's next_retry_at.
                    # Per-row _job_id dedupes if the same row is touched
                    # again before arq pops the deferred job. Without
                    # this push there is no polling cron to catch the
                    # retry — the row would sit forever.
                    if outcome.next_retry_at is not None:
                        await notify_webhook_retry(
                            str(row.id),
                            outcome.next_retry_at,
                            redis=redis,
                        )
        logger.info(
            "webhook_deliver: sent=%d retried=%d failed=%d", sent, retried, failed,
        )
    return {"sent": sent, "retried": retried, "failed": failed}


async def webhook_deliver_task(ctx: dict) -> dict:
    """ARQ-callable wrapper. Records each run in ``cron_run`` for /ops
    visibility and forwards the arq pool from ``ctx`` so retry pushes
    can reuse the existing connection instead of opening a new one."""
    from aggrigator.ops.recorder import cron_run_recorder

    @cron_run_recorder("webhook_deliver")
    async def _runner(ctx_):
        return await run_deliver_due(redis=ctx_.get("redis"))

    return await _runner(ctx)
