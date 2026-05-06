"""Webhook delivery worker — drains due rows, calls send_one, commits.

Triggered by push, NOT a polling cron:
- ``webhooks.notify.notify_webhook_worker`` is called from the ingest
  orchestrator + watchdog right after they commit new delivery rows.
  arq runs this task within milliseconds.
- Failed rows re-enqueue themselves via
  ``webhooks.notify.notify_webhook_retry`` with ``_defer_until=
  next_retry_at``, so retries pop out of arq's delayed-job queue at
  the right moment instead of being polled for.

The task body still SELECTs everything currently due — the queue
trigger just decides *when* to wake up, not *what* to deliver. That
keeps batching simple: one wake-up sweeps all pending rows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from arq.connections import ArqRedis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.config import get_settings
from aggrigator.db import async_session_factory
from aggrigator.models import WebhookDelivery, WebhookEndpoint
from aggrigator.security.webhook_signing import InvalidSignature, decrypt_secret
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
                outcome = await _send_with_endpoint(
                    session, row, http, settings.secret_encryption_key,
                )
                # Commit unconditionally — even the "endpoint disabled" path
                # mutates last_error and we want that persisted.
                await session.commit()
                if outcome is None:
                    failed += 1
                    continue
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


async def _send_with_endpoint(
    session: AsyncSession,
    row: WebhookDelivery,
    http: httpx.AsyncClient,
    encryption_key: str,
):
    endpoint = await session.get(WebhookEndpoint, row.endpoint_id)
    if endpoint is None or not endpoint.enabled:
        # Endpoint deleted/disabled while delivery sat in the queue — mark
        # permanent fail without consuming an attempt.
        row.last_error = "endpoint deleted or disabled"
        row.next_retry_at = None
        return None

    try:
        secret = decrypt_secret(endpoint.secret_ciphertext, key=encryption_key)
    except InvalidSignature as exc:
        # Stored ciphertext was encrypted with a different key (typical when
        # AGG_SECRET_ENCRYPTION_KEY changed, or when a stale row from a test
        # run is sitting in the dev DB). Don't crash the batch — mark this
        # delivery as a permanent failure so the worker keeps draining
        # everything else, and let an operator rotate or clean up.
        logger.warning(
            "delivery=%s endpoint=%s: cannot decrypt secret (%s); "
            "marking permanent_fail. Re-rotate the endpoint's signing "
            "secret via /v1/webhook-endpoints/{id}/rotate to recover.",
            row.id, endpoint.id, exc,
        )
        row.last_error = f"secret decrypt failed: {exc}"
        row.next_retry_at = None
        return None

    return await send_one(
        row, url=endpoint.url, secret=secret, client=http,
    )


async def webhook_deliver_task(ctx: dict) -> dict:
    """ARQ-callable wrapper. Records each run in ``cron_run`` for /ops
    visibility and forwards the arq pool from ``ctx`` so retry pushes
    can reuse the existing connection instead of opening a new one."""
    from aggrigator.ops.recorder import cron_run_recorder

    @cron_run_recorder("webhook_deliver")
    async def _runner(ctx_):
        return await run_deliver_due(redis=ctx_.get("redis"))

    return await _runner(ctx)
