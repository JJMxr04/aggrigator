"""Single source of truth for the registered crons surfaced in the UI.

Each entry maps a stable ``name`` (used as a URL segment + DB key) to:
- the human-readable schedule string the page shows
- the ``runner`` async function that the trigger endpoint calls inline (so the
  ARQ enqueue side and the manual-run side hit identical code paths)
- the ``max_runtime_seconds`` budget — also the Redis lock TTL

Adding a new cron is one entry here plus a ``run_*`` function in
``workers/tasks/``. The HTMX page picks it up automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aggrigator.config import get_settings
from aggrigator.workers.tasks.full_refresh import run_full_refresh
from aggrigator.workers.tasks.ingest import run_ingest_due_leagues
from aggrigator.workers.tasks.seed import run_seed_leagues, run_seed_sports
from aggrigator.workers.tasks.settle import run_settle_pending
from aggrigator.workers.tasks.vacuum import run_vacuum_old_events
from aggrigator.workers.tasks.watchdog import run_lifecycle_watchdog
from aggrigator.workers.tasks.webhook_deliver import run_deliver_due


def _format_ingest_schedule() -> str:
    """Format the actual ``ingest_due_leagues`` cadence based on env vars.

    Read at module-load time. Reflects whatever ``AGG_INGEST_CRON_MINUTES``
    and ``AGG_INGEST_CRON_HOURS`` resolve to on this deployment, so /ops/crons
    shows the real schedule rather than a stale default.
    """
    s = get_settings()
    minutes = sorted(s.ingest_cron_minute_set)
    hours = s.ingest_cron_hour_set  # None = every hour

    if hours is None:
        if minutes == [0]:
            return "hourly @ :00 UTC (env-tunable)"
        if len(minutes) == 1:
            return f"hourly @ :{minutes[0]:02d} UTC (env-tunable)"
        mins = ", ".join(f":{m:02d}" for m in minutes)
        return f"every hour @ {mins} UTC (env-tunable)"

    hours_sorted = sorted(hours)
    if len(hours_sorted) == 1 and len(minutes) == 1:
        return (
            f"daily @ {hours_sorted[0]:02d}:{minutes[0]:02d} UTC (env-tunable)"
        )
    if len(minutes) == 1:
        times = ", ".join(f"{h:02d}:{minutes[0]:02d}" for h in hours_sorted)
        return f"@ {times} UTC (env-tunable)"
    times = ", ".join(
        f"{h:02d}:{m:02d}" for h in hours_sorted for m in minutes
    )
    return f"@ {times} UTC (env-tunable)"


@dataclass
class CronSpec:
    name: str
    description: str
    schedule_human: str          # "every 30 min", "manual only", etc.
    runner: Callable[[], Awaitable[Any]]
    max_runtime_seconds: int


# Order here drives the UI order. ``full_refresh`` sits at the top because
# it's the "one-click everything" path most operators reach for.
REGISTRY: list[CronSpec] = [
    CronSpec(
        name="full_refresh",
        description=(
            "One-shot: seed sports + leagues, then ingest events for every "
            "active league. NOT auto-scheduled — the hourly ingest reaches "
            "steady state on its own. Use after a fresh deploy when you "
            "want to populate everything immediately."
        ),
        schedule_human="manual only",
        runner=run_full_refresh,
        max_runtime_seconds=2400,
    ),
    CronSpec(
        name="seed_sports",
        description=(
            "Upsert the sport list (basketball, football, baseball, …) from "
            "the active provider. Sports change essentially never — runs "
            "weekly, most refreshes are no-ops."
        ),
        schedule_human="weekly Mon @ 01:30 UTC",
        runner=run_seed_sports,
        max_runtime_seconds=120,
    ),
    CronSpec(
        name="seed_leagues",
        description=(
            "Upsert the league list across all sports. Runs daily so newly-"
            "added leagues become walkable on the next ingest tick. "
            "On odds-api.io, only leagues with a mapping in "
            "SGO_TO_ODDSAPI_LEAGUE are persisted — unmapped slugs are "
            "logged and skipped."
        ),
        schedule_human="daily @ 02:00 UTC",
        runner=run_seed_leagues,
        max_runtime_seconds=300,
    ),
    CronSpec(
        name="ingest_due_leagues",
        description=(
            "The combined walk: events + markets + per-bookmaker quotes + "
            "lifecycle + webhooks for every active league. One upstream "
            "call per league plus batched /odds/multi (oddsapi) or embedded "
            "odds (SGO). Provider-aware quota check runs first and skips "
            "the cycle if the threshold is tripped. Cadence tunable via "
            "AGG_INGEST_CRON_MINUTES / AGG_INGEST_CRON_HOURS."
        ),
        schedule_human=_format_ingest_schedule(),
        runner=run_ingest_due_leagues,
        max_runtime_seconds=1800,
    ),
    CronSpec(
        name="webhook_deliver",
        description=(
            "Drain pending webhook deliveries. Push-driven — the ingest "
            "orchestrator + watchdog enqueue this on commit, and failed "
            "deliveries re-enqueue themselves with a deferred job at "
            "next_retry_at. NOT on the auto schedule (would write a "
            "no-op cron_run row every tick). Click Run here only to "
            "recover after a Redis outage that swallowed the push."
        ),
        schedule_human="push-driven (manual recovery)",
        runner=run_deliver_due,
        max_runtime_seconds=120,
    ),
    CronSpec(
        name="settle_pending",
        description=(
            "Settle PENDING selections on finished events (defensive "
            "backfill for anything the hot path missed). Pure DB work, "
            "no upstream calls."
        ),
        schedule_human="nightly @ 03:30 UTC",
        runner=run_settle_pending,
        max_runtime_seconds=900,
    ),
    CronSpec(
        name="vacuum_old_events",
        description=(
            "Delete terminal events older than AGG_VACUUM_DAYS (default 3). "
            "Cascades through markets, selections, quotes, and webhook "
            "deliveries. Frees DB storage on free Postgres tiers."
        ),
        schedule_human="nightly @ 04:00 UTC",
        runner=run_vacuum_old_events,
        max_runtime_seconds=600,
    ),
    CronSpec(
        name="lifecycle_watchdog",
        description=(
            "Two-clock guard against stuck events. "
            "(1) Stale-pending: events past AGG_LIFECYCLE_STALE_GRACE_HOURS "
            "still in notstarted / inprogress — flagged via the API "
            "``stale`` field, optionally auto-VOIDed if "
            "AGG_LIFECYCLE_AUTO_VOID_HOURS > 0. "
            "(2) Disappearance (oddsapi only): events the upstream stopped "
            "returning past AGG_LIFECYCLE_DISAPPEARED_VOID_HOURS get the "
            "same VOID treatment — needed because odds-api.io has no "
            "explicit ``cancelled`` status (cancelled-suspended.md §3)."
        ),
        schedule_human="hourly @ :45",
        runner=run_lifecycle_watchdog,
        max_runtime_seconds=300,
    ),
]


def by_name(name: str) -> CronSpec | None:
    for spec in REGISTRY:
        if spec.name == name:
            return spec
    return None


def names() -> list[str]:
    return [s.name for s in REGISTRY]
