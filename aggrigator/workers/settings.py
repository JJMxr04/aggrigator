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

- ``full_refresh`` — one-shot seed + ingest, used after a fresh deploy.
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
from aggrigator.observability.logging import configure_logging
from aggrigator.workers.tasks.full_refresh import full_refresh_task
from aggrigator.workers.tasks.ingest import (
    ingest_due_leagues_task,
    refresh_existing_events_task,
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
    """Assemble the cron schedule. All times hardcoded — cadence is
    product policy, not infra. To change a time, edit this function and
    redeploy.

    Schedule (top → bottom, by cadence):

      seed_sports             daily 01:30   — catches new sports
      seed_leagues            daily 02:00   — catches new leagues
      ingest_due_leagues      daily 02:30   — DISCOVERY walk: inserts
                                              new events + refreshes
                                              existing
      refresh_existing_events hourly @ :00  — REFRESH walk for events
                                              already in DB; skips the
                                              02:00 slot to avoid racing
                                              the daily discovery
      lifecycle_watchdog      hourly @ :45  — flags stale events;
                                              auto-VOIDs if enabled
      settle_pending          nightly 03:30 — grading backfill
      vacuum_old_events       nightly 04:00 — deletes terminal events
                                              past AGG_VACUUM_DAYS

    Manual-only (registered as ARQ functions but NOT scheduled):

      full_refresh    — one-shot seed + ingest after a fresh deploy.
                        The daily discovery walk reaches the same steady
                        state within a day anyway.
      webhook_deliver — push-driven; the orchestrator + watchdog enqueue
                        it on commit, failed deliveries re-enqueue
                        themselves. A scheduled run would write no-op
                        cron_run rows.
    """
    # Retry budget for the network-bound ingest tasks: arq's default
    # max_tries=1 means a single transient failure (Redis blip, upstream
    # timeout, post-fast-fail rate-limit hit just before bucket reset)
    # leaves a stale cron_run row until the next scheduled tick. With
    # max_tries=2 + the recorder's Retry(defer=300) classifier, one auto
    # retry fires 5 min after a transient failure. Per-league commits
    # mean the retry naturally picks up where the original left off
    # (already-finished leagues UPSERT cheaply; new ones run fresh).
    #
    # job_timeout=900 (15 min) replaces arq's 300s default. A normal
    # walk runs in 5–10s; the headroom absorbs slow upstream responses
    # and the worst-case rate-limit retry budget without the worker
    # cancelling the task mid-league (which is what dropped the row at
    # cron_run id=138 on 2026-05-12).
    INGEST_RETRY_KW = {"timeout": 900, "max_tries": 2}

    return [
        cron(
            seed_sports_task,
            hour={1}, minute={30},
            name="seed_sports",
        ),
        cron(
            seed_leagues_task,
            hour={2}, minute={0},
            name="seed_leagues",
        ),
        cron(
            ingest_due_leagues_task,
            hour={2}, minute={30},
            run_at_startup=False,
            name="ingest_due_leagues",
            **INGEST_RETRY_KW,
        ),
        cron(
            refresh_existing_events_task,
            # Hourly except hour 2, which is the daily discovery slot —
            # avoids both walks racing on the same league.
            hour={0, 1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12,
                  13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23},
            minute={0},
            run_at_startup=False,
            name="refresh_existing_events",
            **INGEST_RETRY_KW,
        ),
        cron(
            lifecycle_watchdog_task,
            # :45 keeps it clear of the refresh tick at :00.
            # DB-only, no provider calls — cadence is not quota-sensitive.
            minute={45},
            name="lifecycle_watchdog",
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


async def _on_startup(ctx: dict) -> None:
    """Loud boot banner — one block, easy to grep in Railway logs.

    Prints the registered cron schedule + Redis target + heartbeat
    cadence so an operator can confirm at a glance that the worker
    booted with the schedule it was supposed to. Pairs with the
    Worker status banner on /ops/crons (which reads the heartbeat
    key written by ``record_health``).
    """
    s = get_settings()
    # Match the API's logging config — same formatter, same level, plus
    # the progress-forward handler so logs from the ingest pipeline
    # surface in /ops/crons via SSE.
    configure_logging(s.log_level)
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
        refresh_existing_events_task,
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
