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

from aggrigator.workers.tasks.full_refresh import run_full_refresh
from aggrigator.workers.tasks.ingest import (
    run_ingest_due_leagues,
    run_ingest_lifecycle_only,
    run_ingest_odds_only,
)
from aggrigator.workers.tasks.seed import run_seed_leagues, run_seed_sports
from aggrigator.workers.tasks.settle import run_settle_pending
from aggrigator.workers.tasks.vacuum import run_vacuum_old_events
from aggrigator.workers.tasks.watchdog import run_lifecycle_watchdog
from aggrigator.workers.tasks.webhook_deliver import run_deliver_due


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
            "active league. NOT auto-scheduled — pure duplication of seed_* "
            "+ hourly ingest_due_leagues. Trigger manually after a fresh "
            "deploy when you want to populate everything immediately."
        ),
        schedule_human="manual only",
        runner=run_full_refresh,
        max_runtime_seconds=2400,
    ),
    CronSpec(
        name="seed_sports",
        description=(
            "Upsert the SGO sport list (basketball, football, baseball, …). "
            "Single SGO call. Sports change essentially never, so this runs "
            "weekly — most refreshes are no-ops."
        ),
        schedule_human="weekly Mon @ 01:30 UTC",
        runner=run_seed_sports,
        max_runtime_seconds=120,
    ),
    CronSpec(
        name="seed_leagues",
        description=(
            "Upsert the SGO league list across all sports (one /leagues "
            "call). Run daily so newly-added leagues become walkable on "
            "the next ingest."
        ),
        schedule_human="daily @ 02:00 UTC",
        runner=run_seed_leagues,
        max_runtime_seconds=300,
    ),
    CronSpec(
        name="ingest_due_leagues",
        description=(
            "Combined walk: events + markets + per-bookmaker quotes + "
            "lifecycle + webhooks in ONE SGO /events call per active "
            "league. SGO bills 1 entity per event returned, so this is "
            "the quota-efficient default — half the burn of a split "
            "lifecycle + odds run. Tune cadence via "
            "AGG_INGEST_CRON_MINUTES / AGG_INGEST_CRON_HOURS."
        ),
        schedule_human="hourly @ :00 (env-tunable)",
        runner=run_ingest_due_leagues,
        max_runtime_seconds=1800,
    ),
    CronSpec(
        name="ingest_event_lifecycle",
        description=(
            "Lightweight variant: event rows + status + settlement + "
            "webhooks. Skips the per-bookmaker quote writes — useful "
            "for a manual 'just refresh statuses' trigger. NOT on the "
            "auto schedule (running this AND ingest_event_odds would "
            "double SGO entity cost vs the combined ingest_due_leagues)."
        ),
        schedule_human="manual only",
        runner=run_ingest_lifecycle_only,
        max_runtime_seconds=600,
    ),
    CronSpec(
        name="ingest_event_odds",
        description=(
            "Markets + per-bookmaker prices for events already in our DB. "
            "Skips lifecycle / webhooks. NOT on the auto schedule — pair "
            "with ingest_event_lifecycle only when you have a specific "
            "reason to keep them split (otherwise use ingest_due_leagues "
            "which does both in one SGO call)."
        ),
        schedule_human="manual only",
        runner=run_ingest_odds_only,
        max_runtime_seconds=1800,
    ),
    CronSpec(
        name="webhook_deliver",
        description="Drain pending webhook deliveries.",
        schedule_human="every 30s",
        runner=run_deliver_due,
        max_runtime_seconds=120,
    ),
    CronSpec(
        name="settle_pending",
        description="Settle PENDING selections on finished events (backfill).",
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
            "Find events past AGG_LIFECYCLE_STALE_GRACE_HOURS (default 12) "
            "still in notstarted/inprogress — usually postponed games SGO "
            "never re-statused. Surfaces them via the API ``stale`` flag. "
            "If AGG_LIFECYCLE_AUTO_VOID_HOURS > 0, events past that longer "
            "threshold are presumptively VOIDED (status_type=canceled, "
            "PENDING selections → VOID, event.voided webhook fires). "
            "Recovery: PROVIDER grading on a later lifecycle pass overrides "
            "the COMPUTED VOID rows if SGO eventually ships scores."
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
