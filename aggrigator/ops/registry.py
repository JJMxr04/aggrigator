"""Single source of truth for the registered crons surfaced in the UI.

Each entry maps a stable ``name`` (used as a URL segment + DB key) to:
- the human-readable schedule string the page shows
- the ``runner`` async function that the manual trigger calls inline (so the
  scheduled-execution side and the manual-run side hit identical code paths)
- the ``task`` Procrastinate task object used by the trigger path to defer
  a job onto the same queue scheduled runs use (so duplicate-click dedup via
  ``queueing_lock`` works for manual triggers too)
- the ``max_runtime_seconds`` budget — informational; surfaced in the UI

Adding a new cron is one entry here plus a ``run_*`` function + Procrastinate
``@app.task`` in ``workers/tasks/``. The HTMX page picks it up automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from procrastinate.tasks import Task

from aggrigator.workers.tasks.full_refresh import full_refresh_task, run_full_refresh
from aggrigator.workers.tasks.ingest import (
    ingest_due_leagues_task,
    refresh_existing_events_task,
    run_ingest_due_leagues,
    run_refresh_existing_events,
)
from aggrigator.workers.tasks.load_registry import (
    load_registry_task,
    run_load_registry,
)
from aggrigator.workers.tasks.seed import (
    run_seed_leagues,
    run_seed_sports,
    seed_leagues_task,
    seed_sports_task,
)
from aggrigator.workers.tasks.settle import run_settle_pending, settle_pending_task
from aggrigator.workers.tasks.vacuum import (
    run_vacuum_old_events,
    vacuum_old_events_task,
)
from aggrigator.workers.tasks.watchdog import (
    lifecycle_watchdog_task,
    run_lifecycle_watchdog,
)
from aggrigator.workers.tasks.webhook_deliver import (
    run_deliver_due,
    webhook_deliver_task,
)


@dataclass
class CronSpec:
    name: str
    description: str
    schedule_human: str          # "every 30 min", "manual only", etc.
    runner: Callable[[], Awaitable[Any]]
    task: Task                   # Procrastinate task — used by the trigger path
    max_runtime_seconds: int
    # True if this cron is wired into Procrastinate's periodic schedule
    # via ``@app.periodic`` on the task. Drives whether the /ops/crons
    # toggle is rendered — manual-only crons (full_refresh, load_registry,
    # webhook_deliver, ...) have no scheduled tick to pause.
    is_scheduled: bool = False


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
        task=full_refresh_task,
        max_runtime_seconds=2400,
    ),
    CronSpec(
        name="load_registry",
        description=(
            "Validate and apply the on-disk team/league/sport registry "
            "under ``aggrigator/data/sports/``. Annotates Sport/League "
            "rows with provider keys (odds_api_io_key, thesportsdb_id), "
            "upserts canonical Team rows (including roster_filler entries "
            "with both keys NULL), and recomputes "
            "``League.can_pull_historical_scores`` per league. Requires "
            "``seed_sports``/``seed_leagues`` to have run first — Sport "
            "and League rows must exist before the registry can annotate "
            "them. Re-run after editing the JSON files on disk."
        ),
        schedule_human="manual only",
        runner=run_load_registry,
        task=load_registry_task,
        max_runtime_seconds=120,
    ),
    CronSpec(
        name="seed_sports",
        description=(
            "Upsert the sport list (basketball, football, baseball, …) from "
            "the active provider. Runs daily so newly-added sports land "
            "in time for the next discovery walk; most ticks are no-ops."
        ),
        schedule_human="daily @ 01:30 UTC",
        runner=run_seed_sports,
        task=seed_sports_task,
        max_runtime_seconds=120,
        is_scheduled=True,
    ),
    CronSpec(
        name="seed_leagues",
        description=(
            "Upsert leagues whose parent Sport is ``active=True`` in the "
            "DB. Sports default to inactive — flip one on in SQLAdmin and "
            "re-run this to bring its leagues in (new leagues default to "
            "``active=True``; ingest then requires active league AND "
            "active sport). Runs daily so newly-added leagues land in "
            "time for the next ingest tick. On odds-api.io, only leagues "
            "with a mapping in INTERNAL_TO_ODDSAPI_LEAGUE are persisted — "
            "unmapped slugs are logged and skipped."
        ),
        schedule_human="daily @ 02:00 UTC",
        runner=run_seed_leagues,
        task=seed_leagues_task,
        max_runtime_seconds=300,
        is_scheduled=True,
    ),
    CronSpec(
        name="ingest_due_leagues",
        description=(
            "DISCOVERY walk: events + markets + per-bookmaker quotes + "
            "lifecycle + webhooks for every active league whose parent "
            "sport is also active (League.active AND Sport.active). "
            "Inserts new events the provider has surfaced AND refreshes "
            "everything already in DB. Runs once daily — intra-day "
            "refreshes are handled by ``refresh_existing_events`` which "
            "skips new-event inserts."
        ),
        schedule_human="daily @ 02:30 UTC",
        runner=run_ingest_due_leagues,
        task=ingest_due_leagues_task,
        max_runtime_seconds=1800,
        is_scheduled=True,
    ),
    CronSpec(
        name="refresh_existing_events",
        description=(
            "REFRESH walk for events ALREADY in DB: odds + scores + "
            "lifecycle. Skips events not yet in DB — those arrive on the "
            "daily ``ingest_due_leagues`` discovery cron. Cheaper "
            "per-event than discovery (no Team/Event insert paths). Runs "
            "hourly @ :00 except hour 2, which collides with discovery."
        ),
        schedule_human="hourly @ :00 UTC (skips 02:00)",
        runner=run_refresh_existing_events,
        task=refresh_existing_events_task,
        max_runtime_seconds=1800,
        is_scheduled=True,
    ),
    CronSpec(
        name="webhook_deliver",
        description=(
            "Drain pending webhook deliveries. Push-driven — the ingest "
            "orchestrator + watchdog defer this on commit, and failed "
            "deliveries re-defer themselves at next_retry_at. NOT on the "
            "auto schedule (would write a no-op cron_run row every tick). "
            "Click Run here only to recover from a backlog."
        ),
        schedule_human="push-driven (manual recovery)",
        runner=run_deliver_due,
        task=webhook_deliver_task,
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
        task=settle_pending_task,
        max_runtime_seconds=900,
        is_scheduled=True,
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
        task=vacuum_old_events_task,
        max_runtime_seconds=600,
        is_scheduled=True,
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
            "explicit ``cancelled`` status."
        ),
        schedule_human="hourly @ :45",
        runner=run_lifecycle_watchdog,
        task=lifecycle_watchdog_task,
        max_runtime_seconds=300,
        is_scheduled=True,
    ),
]


def by_name(name: str) -> CronSpec | None:
    for spec in REGISTRY:
        if spec.name == name:
            return spec
    return None


def names() -> list[str]:
    return [s.name for s in REGISTRY]
