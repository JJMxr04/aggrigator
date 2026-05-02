"""ARQ worker config.

Run with::

    arq aggrigator.workers.settings.WorkerSettings

The schedule mirrors plan §5: ingest every 30m by default, webhook_deliver
every 30s (short loop because retries clump near boundaries), settle nightly
at 03:30 UTC. The ingest + full_refresh cadences are env-tunable for free
SGO tier — see ``AGG_INGEST_CRON_MINUTES`` and ``AGG_FULL_REFRESH_WEEKDAY``
in config.py.
"""

from __future__ import annotations

from arq.connections import RedisSettings
from arq.cron import cron

from aggrigator.config import get_settings
from aggrigator.workers.tasks.full_refresh import full_refresh_task
from aggrigator.workers.tasks.ingest import (
    ingest_due_leagues_task,
    ingest_lifecycle_only_task,
    ingest_odds_only_task,
)
from aggrigator.workers.tasks.seed import seed_leagues_task, seed_sports_task
from aggrigator.workers.tasks.settle import settle_pending_task
from aggrigator.workers.tasks.vacuum import vacuum_old_events_task
from aggrigator.workers.tasks.webhook_deliver import webhook_deliver_task


def _redis_settings() -> RedisSettings:
    s = get_settings()
    return RedisSettings.from_dsn(s.redis_url)


def _build_cron_jobs() -> list:
    """Assemble the cron schedule, honoring env-driven cadence knobs.

    Done at module load, not inside WorkerSettings, so arq sees a plain
    ``cron_jobs`` attribute. Re-importing this module after changing env
    vars yields a fresh schedule — that matches the worker's
    deploy-on-restart lifecycle.
    """
    s = get_settings()

    # full_refresh: daily 02:30 UTC by default; weekly when
    # AGG_FULL_REFRESH_WEEKDAY is set (free-tier friendly).
    full_refresh_kwargs: dict = {
        "hour": {2},
        "minute": {30},
        "name": "full_refresh",
    }
    if s.full_refresh_weekday_set is not None:
        full_refresh_kwargs["weekday"] = s.full_refresh_weekday_set

    return [
        cron(
            seed_sports_task,
            # Sports change essentially never. Weekly Mondays is enough
            # to catch SGO adding a brand-new sport.
            weekday={0}, hour={1}, minute={30},
            name="seed_sports",
        ),
        cron(
            seed_leagues_task,
            # Leagues drift more often than sports (new minor leagues,
            # season transitions). Daily so newly-added leagues become
            # walkable on the next ingest.
            hour={2}, minute={0},
            name="seed_leagues",
        ),
        cron(full_refresh_task, **full_refresh_kwargs),
        cron(
            ingest_due_leagues_task,
            minute=s.ingest_cron_minute_set,
            run_at_startup=False,
            name="ingest_due_leagues",
        ),
        cron(
            webhook_deliver_task,
            second={0, 30},
            name="webhook_deliver",
        ),
        cron(
            settle_pending_task,
            hour={3}, minute={30},
            name="settle_pending",
        ),
        cron(
            vacuum_old_events_task,
            hour={4}, minute={0},  # 30 min after settle_pending finishes
            name="vacuum_old_events",
        ),
    ]


class WorkerSettings:
    redis_settings = _redis_settings()

    # Ad-hoc enqueue targets (used when the API layer wants to trigger a
    # job out-of-band, e.g. on-demand event refresh).
    functions = [
        full_refresh_task,
        ingest_due_leagues_task,
        ingest_lifecycle_only_task,
        ingest_odds_only_task,
        webhook_deliver_task,
        settle_pending_task,
        seed_sports_task,
        seed_leagues_task,
        vacuum_old_events_task,
    ]

    cron_jobs = _build_cron_jobs()

    keep_result = 3600  # one hour of job results visible in Redis
    max_jobs = 10
