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
- ``webhook_deliver`` — push-driven, no schedule. The orchestrator +
  watchdog enqueue this task right after they commit new delivery
  rows, and failed deliveries re-enqueue themselves with
  ``_defer_until=next_retry_at``. The cron stays registered so
  operators can recover from a Redis outage by clicking Run once
  Redis is back, but it never auto-fires (would write a no-op
  ``cron_run`` row every tick).
"""

from __future__ import annotations

import logging

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

logger = logging.getLogger(__name__)


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
    #   webhook_deliver     — push-driven (see module docstring + webhooks/notify).
    #                          The orchestrator/watchdog enqueue it on commit
    #                          and failed deliveries re-enqueue themselves with
    #                          _defer_until=next_retry_at. A scheduled run would
    #                          just write no-op cron_run rows.
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
        # webhook_deliver is intentionally NOT on the auto schedule —
        # it's push-driven (see module docstring). Still registered as a
        # ``functions`` target below so push-enqueues + manual /ops/crons
        # triggers work.
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


async def _on_startup(ctx: dict) -> None:
    """Loud boot banner — one block, easy to grep in Railway logs.

    Prints the registered cron schedule + Redis target + heartbeat
    cadence so an operator can confirm at a glance that the worker
    booted with the schedule it was supposed to. Pairs with the
    Worker status banner on /ops/crons (which reads the heartbeat
    key written by ``record_health``).
    """
    s = get_settings()
    lines = [
        "=" * 64,
        "[arq-worker] booted — cron schedule registered:",
    ]
    for job in WorkerSettings.cron_jobs:
        lines.append(f"  - {job.name:<22}  {_cron_human(job)}")
    lines.append(
        f"[arq-worker] redis={_redacted(s.redis_url)}  "
        f"queue=arq:queue  heartbeat_every={WorkerSettings.health_check_interval}s"
    )
    lines.append(
        "[arq-worker] heartbeat key: arq:queue:health-check "
        "(read by /ops/crons banner + /v1/admin/crons/worker-status)"
    )
    lines.append("=" * 64)
    for line in lines:
        logger.info(line)


def _cron_human(job) -> str:
    """Best-effort one-line summary of an arq CronJob's schedule."""
    parts = []
    for attr in ("month", "day", "weekday", "hour", "minute", "second"):
        val = getattr(job, attr, None)
        if val is None:
            continue
        if isinstance(val, set):
            shown = ",".join(str(v) for v in sorted(val))
        else:
            shown = str(val)
        parts.append(f"{attr}={shown}")
    return " ".join(parts) if parts else "(every tick)"


def _redacted(url: str) -> str:
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    user = creds.split(":", 1)[0] if ":" in creds else creds
    return f"{scheme}://{user}:***@{host}"


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

    # Heartbeat cadence — arq's default is 3600s (one hour), which is
    # useless for a live "is the worker alive?" dashboard. 30s gives the
    # /ops/crons banner sub-minute fidelity. The key TTL arq sets is
    # (interval+1)*1000ms, so the key disappears within ~31s of the
    # worker dying — that's our "OFFLINE" signal.
    health_check_interval = 30

    on_startup = _on_startup
