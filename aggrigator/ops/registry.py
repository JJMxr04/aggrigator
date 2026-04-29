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
from aggrigator.workers.tasks.ingest import run_ingest_due_leagues
from aggrigator.workers.tasks.seed import run_seed_sports_and_leagues
from aggrigator.workers.tasks.settle import run_settle_pending
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
            "Seed sports + leagues, then ingest events for every active "
            "league. The end-to-end one-click refresh."
        ),
        schedule_human="daily @ 02:30 UTC",
        runner=run_full_refresh,
        max_runtime_seconds=2400,
    ),
    CronSpec(
        name="seed_sports_and_leagues",
        description=(
            "Pull every sport and league from SGO into the DB. Doesn't fetch "
            "events — use ``full_refresh`` or ``ingest_due_leagues`` for that."
        ),
        schedule_human="daily @ 02:00 UTC",
        runner=run_seed_sports_and_leagues,
        max_runtime_seconds=600,
    ),
    CronSpec(
        name="ingest_due_leagues",
        description=(
            "Walk every active league once and ingest events. Assumes seed "
            "has already run; otherwise no leagues are active and this is a "
            "no-op."
        ),
        schedule_human="every 30 min",
        runner=run_ingest_due_leagues,
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
]


def by_name(name: str) -> CronSpec | None:
    for spec in REGISTRY:
        if spec.name == name:
            return spec
    return None


def names() -> list[str]:
    return [s.name for s in REGISTRY]
