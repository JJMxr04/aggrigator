"""Push-on-write trigger for the webhook delivery worker.

The aggregator used to run a ``webhook_deliver`` cron every 30 seconds
to drain rows out of ``webhook_delivery``. That poller fired ~2,880
times per day even when there was nothing to send, and every tick
wrote a ``cron_run`` row — clogging the history table with no-ops.

The new model is fully event-driven:

- **Push on write.** The moment a delivery row is committed, we enqueue
  a single arq job (``webhook_deliver_task``) via
  ``notify_webhook_worker``. The worker drains it within milliseconds.
  Multiple writes within the same drain window coalesce on a fixed
  ``_job_id`` so a burst of 50 inserts collapses to one queued job.

- **Push on retry.** When a delivery fails and ``next_retry_at`` gets
  set, the worker enqueues a *deferred* job via
  ``notify_webhook_retry`` (``_defer_until=next_retry_at``). arq holds
  it in Redis's delayed-job sorted set until the retry moment, then
  pops it. Per-delivery ``_job_id`` prevents accidental double-enqueue.

There is NO scheduled fallback. If Redis is unreachable at write time,
the row sits in Postgres and an operator can manually trigger
``webhook_deliver`` from /ops/crons (the cron stays registered for
manual runs even though it's not on the auto schedule). The
worker's ``on_startup`` hook also kicks off a one-shot drain so any
rows that accumulated during a worker outage are picked up on boot.

Failure handling
----------------
Notify failures are NEVER fatal to the caller. If Redis is unreachable,
we log a warning and return ``False``. The row is already committed in
Postgres — the next successful push (or a manual /ops/crons trigger,
or the worker's startup drain) will catch up. Crashing the ingest path
because we couldn't enqueue would be much worse.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from aggrigator.config import get_settings

logger = logging.getLogger(__name__)

# Coalescing key for the "drain now" path. All push-on-write callers use
# the same ID so concurrent calls from the orchestrator + watchdog +
# manual triggers collapse into one queued job.
WEBHOOK_DELIVER_JOB_ID = "webhook_deliver:due"

# Process-local arq pool, lazily created on first notify from this
# process and reused for the lifetime of the process. The web service
# (FastAPI) and the worker each get their own pool — that's fine,
# both end up writing to the same Redis queue.
_pool: Optional[ArqRedis] = None


async def notify_webhook_worker(redis: Optional[ArqRedis] = None) -> bool:
    """Enqueue a coalescing ``webhook_deliver_task`` to drain pending rows.

    Args:
        redis: An existing arq pool (e.g. ``ctx['redis']`` inside an
            arq task) to reuse. If ``None``, the helper lazily creates
            one from ``settings.redis_url`` and caches it module-wide.

    Returns:
        ``True`` if the enqueue succeeded (or was deduped — both mean the
        job is queued), ``False`` if Redis was unreachable. The row is
        already in Postgres so a ``False`` return is non-fatal; an
        operator can recover with a manual trigger from /ops/crons.
    """
    pool = await _resolve_pool(redis)
    if pool is None:
        return False

    try:
        # _job_id is the coalescing key. arq's enqueue_job returns None
        # when a job with this ID is already queued — that's the intended
        # "one is already in flight, no need to enqueue another" path,
        # NOT an error.
        job = await pool.enqueue_job(
            "webhook_deliver_task",
            _job_id=WEBHOOK_DELIVER_JOB_ID,
        )
        if job is None:
            logger.debug(
                "notify_webhook_worker: deduped — a webhook_deliver job "
                "is already queued."
            )
        return True
    except Exception as exc:  # noqa: BLE001 — never fatal
        logger.warning(
            "notify_webhook_worker: enqueue failed (%s: %s) — row remains "
            "in Postgres; recover via manual /ops/crons trigger.",
            type(exc).__name__, exc,
        )
        return False


async def notify_webhook_retry(
    delivery_id: str,
    next_retry_at: datetime,
    *,
    redis: Optional[ArqRedis] = None,
) -> bool:
    """Enqueue a deferred ``webhook_deliver_task`` for a failed delivery.

    Called from the worker after ``send_one`` sets ``next_retry_at`` on
    a failed row. arq parks the job in its delayed-job sorted set and
    pops it at ``next_retry_at`` — no polling cron required.

    Args:
        delivery_id: UUID of the failed delivery. Used as part of the
            ``_job_id`` so re-enqueues for the same row dedupe (e.g. if
            the same delivery fails twice in adjacent batches).
        next_retry_at: When arq should run the job. Naive datetimes are
            interpreted as UTC by arq; pass tz-aware to be explicit.
        redis: An existing arq pool to reuse, otherwise lazy-init.

    Returns:
        ``True`` on enqueue success / dedup, ``False`` on Redis failure.
        Non-fatal — the row's ``next_retry_at`` is set in Postgres, so
        a future ``notify_webhook_worker`` call (e.g. from the next
        successful ingest) will sweep this row when ``next_retry_at``
        is in the past.
    """
    pool = await _resolve_pool(redis)
    if pool is None:
        return False

    try:
        # Per-delivery _job_id. The job body is still ``run_deliver_due``,
        # which sweeps everything pending — the delivery_id in the key
        # is purely for dedup, not routing.
        job = await pool.enqueue_job(
            "webhook_deliver_task",
            _job_id=f"webhook_deliver:retry:{delivery_id}",
            _defer_until=next_retry_at,
        )
        if job is None:
            logger.debug(
                "notify_webhook_retry: deduped — retry for delivery=%s "
                "already deferred.", delivery_id,
            )
        return True
    except Exception as exc:  # noqa: BLE001 — never fatal
        logger.warning(
            "notify_webhook_retry: enqueue failed for delivery=%s "
            "(%s: %s) — next_retry_at is set in Postgres; recover via "
            "a later push or manual /ops/crons trigger.",
            delivery_id, type(exc).__name__, exc,
        )
        return False


async def _resolve_pool(redis: Optional[ArqRedis]) -> Optional[ArqRedis]:
    if redis is not None:
        return redis
    try:
        return await _get_pool()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "notify: could not obtain arq pool (%s: %s) — webhook push "
            "skipped, recover via manual /ops/crons trigger.",
            type(exc).__name__, exc,
        )
        return None


async def _get_pool() -> ArqRedis:
    """Lazily create + cache a process-local arq pool.

    arq's ``create_pool`` reuses an underlying redis-py connection pool,
    so re-creating per-call would be wasteful. Caching at module scope
    matches the lifetime of the gunicorn worker / arq worker process.
    """
    global _pool
    if _pool is None:
        s = get_settings()
        _pool = await create_pool(RedisSettings.from_dsn(s.redis_url))
    return _pool


async def close_pool() -> None:
    """Tear down the cached pool. Used in tests; production processes
    let the pool live for the process lifetime."""
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
