"""Composite task — seed sports + leagues, then ingest events for every active
league. The natural "one button gives me everything" entry point.

When the operator clicks "Refresh all" (or the cron fires), this:

1. Pulls sports + leagues from SGO, upserts them. New rows default to
   ``active=True`` (per ``upsert_sport_from_sgo`` / ``upsert_league_from_sgo``)
   so the freshly-seeded leagues are immediately walkable.
2. Walks every active league once and ingests events + markets + selections
   + per-bookmaker quotes. Lifecycle transitions enqueue webhook deliveries
   automatically (orchestrator handles that — plan §3).

Returns a combined summary so the cron-run row records both halves.

If step 1 fails, step 2 is skipped (the ingester needs the leagues to exist).
If step 2 fails after step 1 succeeded, the seed result is still recorded —
the failure surfaces in ``cron_run.error`` for the run as a whole.
"""

from __future__ import annotations

import logging

from aggrigator.config import get_settings
from aggrigator.ingest.quota import quota_status
from aggrigator.ops.progress import set_progress
from aggrigator.workers.tasks.ingest import _build_client, run_ingest_due_leagues
from aggrigator.workers.tasks.seed import run_seed_sports_and_leagues

logger = logging.getLogger(__name__)


async def run_full_refresh() -> dict:
    # Pre-flight quota check — skip BOTH halves when over budget, so we
    # don't burn the seed allowance (~5–15 calls) only to bail in the
    # ingest step. Uses the same proportional pacer as ``_run_ingest``
    # so a daily auto full_refresh doesn't burn the monthly entity cap.
    settings = get_settings()
    qs = quota_status(
        _build_client(),
        threshold_pct=settings.sgo_quota_threshold_pct,
        reset_day=settings.sgo_quota_reset_day,
        pace_floor_pct=settings.sgo_quota_pace_floor_pct,
    )
    for line in qs.summary_lines:
        await set_progress(line)
    if not settings.test_mode and qs.should_skip:
        reason = "sgo_monthly_quota" if qs.exhausted else "sgo_quota_pace"
        await set_progress(
            f"full_refresh: SKIPPED — {qs.pace_reason if not qs.exhausted else 'absolute cap reached'}"
        )
        return {"skipped": True, "reason": reason, "pace_reason": qs.pace_reason}

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


async def full_refresh_task(ctx: dict) -> dict:
    """ARQ-callable wrapper. Records each scheduled run in ``cron_run``."""
    from aggrigator.ops.recorder import cron_run_recorder

    @cron_run_recorder("full_refresh")
    async def _runner(ctx_):
        return await run_full_refresh()

    return await _runner(ctx)
