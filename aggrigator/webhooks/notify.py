"""Push-on-write trigger for the webhook delivery worker.

The aggregator used to run a ``webhook_deliver`` cron every 30 seconds
to drain rows out of ``webhook_delivery``. That poller fired ~2,880
times per day even when there was nothing to send, and every tick
wrote a ``cron_run`` row — clogging the history table with no-ops.

The new model is fully event-driven, backed by Procrastinate (Postgres
LISTEN/NOTIFY for push-based job pickup; no Redis):

- **Push on write.** The moment a delivery row is committed, we defer
  a single ``webhook_deliver_task`` job via ``notify_webhook_worker``.
  The worker picks it up within milliseconds. Multiple writes within
  the same drain window coalesce on the fixed ``queueing_lock``
  ``"webhook_deliver:due"`` so a burst of 50 inserts collapses to one
  queued job.

- **Push on retry.** When a delivery fails and ``next_retry_at`` gets
  set, the worker defers a *future* job via ``notify_webhook_retry``
  (``schedule_at=next_retry_at``). Procrastinate holds it in the
  ``procrastinate_jobs`` table with ``scheduled_at`` set and surfaces
  it to the worker exactly when due. A per-delivery ``queueing_lock``
  prevents accidental double-enqueue.

There is NO scheduled fallback. If the queue is unreachable at write
time, the row sits in Postgres and an operator can manually trigger
``webhook_deliver`` from /ops/crons (the cron stays registered for
manual runs even though it's not on the auto schedule).

Failure handling
----------------
Notify failures are NEVER fatal to the caller. If the queue insert
fails, we log a warning and return ``False``. The row is already
committed in the source-of-truth table — the next successful push
(or a manual /ops/crons trigger) will catch up. Crashing the ingest
path because we couldn't enqueue would be much worse.
"""

from __future__ import annotations

import logging
from datetime import datetime

from procrastinate.exceptions import AlreadyEnqueued

logger = logging.getLogger(__name__)

# Coalescing key for the "drain now" path. All push-on-write callers use
# the same lock so concurrent calls from the orchestrator + watchdog +
# manual triggers collapse into one queued job.
WEBHOOK_DELIVER_LOCK = "webhook_deliver:due"


async def notify_webhook_worker() -> bool:
    """Defer a coalescing ``webhook_deliver_task`` to drain pending rows.

    Returns ``True`` if the defer succeeded (or was deduped — both mean
    a job is queued), ``False`` if Procrastinate rejected the call (only
    expected when the queue subsystem is unreachable; the
    ``AlreadyEnqueued`` dedup case is reported as success). The row is
    already in Postgres so a ``False`` return is non-fatal; an operator
    can recover with a manual trigger from /ops/crons.
    """
    # Imported lazily — webhook_deliver imports notify for the retry
    # path, so a top-level import here would create a cycle.
    from aggrigator.workers.tasks.webhook_deliver import webhook_deliver_task

    try:
        await webhook_deliver_task.configure(
            queueing_lock=WEBHOOK_DELIVER_LOCK,
        ).defer_async()
        return True
    except AlreadyEnqueued:
        logger.debug(
            "notify_webhook_worker: deduped — a webhook_deliver job is "
            "already queued."
        )
        return True
    except Exception as exc:  # noqa: BLE001 — never fatal
        logger.warning(
            "notify_webhook_worker: defer failed (%s: %s) — row remains "
            "in Postgres; recover via manual /ops/crons trigger.",
            type(exc).__name__, exc,
        )
        return False


async def notify_webhook_retry(
    delivery_id: str,
    next_retry_at: datetime,
) -> bool:
    """Defer a future ``webhook_deliver_task`` for a failed delivery.

    Called from the worker after ``send_one`` sets ``next_retry_at`` on
    a failed row. Procrastinate stores the job with ``scheduled_at`` set
    and dispatches it when due — no polling cron required.

    Args:
        delivery_id: UUID of the failed delivery. Used as the
            ``queueing_lock`` so re-enqueues for the same row dedupe
            (e.g. if the same delivery fails twice in adjacent batches).
        next_retry_at: When Procrastinate should run the job. Pass
            tz-aware (UTC) so the comparison is unambiguous.

    Returns:
        ``True`` on defer success / dedup, ``False`` on queue failure.
        Non-fatal — the row's ``next_retry_at`` is set in Postgres, so
        a future ``notify_webhook_worker`` call will sweep this row
        when ``next_retry_at`` is in the past.
    """
    from aggrigator.workers.tasks.webhook_deliver import webhook_deliver_task

    try:
        await webhook_deliver_task.configure(
            queueing_lock=f"webhook_deliver:retry:{delivery_id}",
            schedule_at=next_retry_at,
        ).defer_async()
        return True
    except AlreadyEnqueued:
        logger.debug(
            "notify_webhook_retry: deduped — retry for delivery=%s "
            "already deferred.", delivery_id,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — never fatal
        logger.warning(
            "notify_webhook_retry: defer failed for delivery=%s "
            "(%s: %s) — next_retry_at is set in Postgres; recover via "
            "a later push or manual /ops/crons trigger.",
            delivery_id, type(exc).__name__, exc,
        )
        return False
