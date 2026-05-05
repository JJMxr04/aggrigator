"""ARQ worker config.

Run with::

    arq aggrigator.workers.settings.WorkerSettings

Auto-scheduled crons are sized for SGO's per-month entity cap (1 entity
per event returned). The combined ``ingest_due_leagues`` runs hourly by
default — half the entity burn of the old lifecycle + odds split, which
billed every event twice per cycle. Tune cadence via
``AGG_INGEST_CRON_MINUTES`` / ``AGG_INGEST_CRON_HOURS`` in config.py.

Manual-only (still callable via ``/ops/crons`` and as ad-hoc enqueue
targets, but NOT on the auto schedule):

- ``full_refresh`` — pure duplication of seed_* + ingest_due_leagues now
  that ingest is hourly. Useful for one-shot post-deploy population.
- ``ingest_event_lifecycle`` / ``ingest_event_odds`` — splitting these
  doubles SGO entity cost vs the combined ``ingest_due_leagues``.
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
from aggrigator.workers.tasks.watchdog import lifecycle_watchdog_task
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

    # Auto schedule (top → bottom, by cadence):
    #   seed_sports         weekly Mon 01:30 — taxonomy almost never changes
    #   seed_leagues        daily 02:00      — catches new leagues / season starts
    #   ingest_due_leagues  hourly @ :00     — combined event + status + odds +
    #                                          settle + webhooks in ONE /events
    #                                          call per league (1 SGO entity per
    #                                          returned event); env-tunable
    #   lifecycle_watchdog  hourly @ :45     — flags stale events (notstarted past
    #                                          start_time); auto-VOIDs if enabled
    #   webhook_deliver     every 30s        — drains pending outbound deliveries
    #   settle_pending      nightly 03:30    — backfill for selections the hot
    #                                          path missed (rare, defensive)
    #   vacuum_old_events   nightly 04:00    — deletes terminal events past
    #                                          AGG_VACUUM_DAYS
    #
    # Removed from auto schedule (still callable manually via /ops/crons):
    #   full_refresh        — pure duplication of seed_* + ingest_due_leagues now
    #                          that ingest is hourly. ~1 walk's worth of entities
    #                          per fire, no added value. Trigger manually after
    #                          a fresh deploy if you want to populate everything
    #                          immediately.
    #   ingest_event_lifecycle / ingest_event_odds — splitting these doubles SGO
    #                          entity cost vs the combined ingest_due_leagues.
    ingest_kwargs: dict = {
        "minute": s.ingest_cron_minute_set,
        "run_at_startup": False,
        "name": "ingest_due_leagues",
    }
    if s.ingest_cron_hour_set is not None:
        ingest_kwargs["hour"] = s.ingest_cron_hour_set

    return [
        cron(
            seed_sports_task,
            weekday={0}, hour={1}, minute={30},
            name="seed_sports",
        ),
        cron(
            seed_leagues_task,
            hour={2}, minute={0},
            name="seed_leagues",
        ),
        cron(ingest_due_leagues_task, **ingest_kwargs),
        cron(
            lifecycle_watchdog_task,
            # :45 keeps it clear of the default ingest tick at :00.
            # DB-only, no SGO calls — cadence is not quota-sensitive.
            minute={45},
            name="lifecycle_watchdog",
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
            hour={4}, minute={0},
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
        lifecycle_watchdog_task,
    ]

    cron_jobs = _build_cron_jobs()

    keep_result = 3600  # one hour of job results visible in Redis
    max_jobs = 10
