"""ARQ worker config.

Run with::

    arq aggrigator.workers.settings.WorkerSettings

The schedule mirrors plan §5: ingest every 30m, webhook_deliver every 30s
(short loop because retries clump near boundaries), settle nightly at 03:30 UTC.
"""

from __future__ import annotations

from arq.connections import RedisSettings
from arq.cron import cron

from aggrigator.config import get_settings
from aggrigator.workers.tasks.full_refresh import full_refresh_task
from aggrigator.workers.tasks.ingest import ingest_due_leagues_task
from aggrigator.workers.tasks.seed import seed_task
from aggrigator.workers.tasks.settle import settle_pending_task
from aggrigator.workers.tasks.webhook_deliver import webhook_deliver_task


def _redis_settings() -> RedisSettings:
    s = get_settings()
    return RedisSettings.from_dsn(s.redis_url)


class WorkerSettings:
    redis_settings = _redis_settings()

    # Ad-hoc enqueue targets (used when the API layer wants to trigger a
    # job out-of-band, e.g. on-demand event refresh).
    functions = [
        full_refresh_task,
        ingest_due_leagues_task,
        webhook_deliver_task,
        settle_pending_task,
        seed_task,
    ]

    # Cron schedule.
    cron_jobs = [
        cron(
            seed_task,
            hour={2}, minute={0},  # daily 02:00 UTC — refresh sport/league taxonomy
            name="seed_sports_and_leagues",
        ),
        cron(
            full_refresh_task,
            hour={2}, minute={30}, # daily 02:30 UTC — seed (again, idempotent)
                                   # then ingest events for every active league
            name="full_refresh",
        ),
        cron(
            ingest_due_leagues_task,
            minute={0, 30},        # every 30 minutes
            run_at_startup=False,
            name="ingest_due_leagues",
        ),
        cron(
            webhook_deliver_task,
            second={0, 30},        # every 30 seconds
            name="webhook_deliver",
        ),
        cron(
            settle_pending_task,
            hour={3}, minute={30}, # nightly 03:30 UTC
            name="settle_pending",
        ),
    ]

    keep_result = 3600  # one hour of job results visible in Redis
    max_jobs = 10
