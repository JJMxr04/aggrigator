"""Composite task — seed sports + leagues, then ingest events for every
league whose League AND Sport are both active. The natural "one button
gives me everything" entry point.

When the operator clicks "Refresh all" (or the cron fires), this:

1. Pulls sports + leagues from the upstream provider and upserts them.
   New Sport rows default to ``active=False`` (operator gate); new
   League rows default to ``active=True`` but are filtered at seed
   time to those whose parent Sport is already active. On a fresh DB
   with no enabled sports, this step inserts the sports inactive and
   inserts zero leagues until an operator flips a sport on and
   re-runs.
2. Walks every league where ``League.active AND Sport.active`` and
   ingests events + markets + selections + per-bookmaker quotes.
   Lifecycle transitions enqueue webhook deliveries automatically
   (orchestrator handles that — plan §3).

Returns a combined summary so the cron-run row records both halves.

If step 1 fails, step 2 is skipped (the ingester needs the leagues to exist).
If step 2 fails after step 1 succeeded, the seed result is still recorded —
the failure surfaces in ``cron_run.error`` for the run as a whole.
"""

from __future__ import annotations

import logging

from aggrigator.config import get_settings
from aggrigator.ingest.odds_api_quota import quota_status
from aggrigator.ops.recorder import cron_run_recorder
from aggrigator.workers.app import app
from aggrigator.workers.tasks.ingest import _build_client, run_ingest_due_leagues
from aggrigator.workers.tasks.seed import run_seed_sports_and_leagues

logger = logging.getLogger(__name__)


async def run_full_refresh() -> dict:
    # Pre-flight quota check — skip BOTH halves when over budget, so we
    # don't burn the seed allowance only to bail in the ingest step.
    settings = get_settings()
    qs = quota_status(
        _build_client(),
        threshold_pct=settings.odds_api_throttle_pct,
    )
    for line in qs.summary_lines:
        logger.info("full_refresh: quota — %s", line)
    if not settings.test_mode and qs.should_skip:
        logger.warning("full_refresh: SKIPPED — %s", qs.pace_reason)
        return {
            "skipped": True,
            "reason": "oddsapi_per_hour_quota",
            "pace_reason": qs.pace_reason,
        }

    seed_summary = await run_seed_sports_and_leagues()
    ingest_summary = await run_ingest_due_leagues()
    combined = {
        "seed": seed_summary,
        "ingest": ingest_summary,
        # Top-level events_processed mirrors the ingest count so the
        # cron_run.items_processed column shows a meaningful number on the
        # ops-console card (the recorder picks the first matching key —
        # see recorder.close_run).
        "events_processed": ingest_summary.get("events_processed", 0),
    }
    logger.info("full_refresh report: %s", combined)
    return combined


# Manual-only — no ``@app.periodic``. The hourly ingest walk reaches the
# same steady state on its own; ``full_refresh`` exists for operators to
# click after a fresh deploy. ``queueing_lock`` matches the cron name so
# double-clicks dedup the same way scheduled tasks do.
@app.task(
    name="aggrigator.full_refresh",
    pass_context=True,
    queueing_lock="full_refresh",
)
@cron_run_recorder("full_refresh")
async def full_refresh_task(context):
    return await run_full_refresh()
