"""Ingest tasks — runnable both as ARQ jobs and as plain async functions
(so tests can call them without a Redis dependency)."""

from __future__ import annotations

import logging
from typing import Any

from aggrigator.config import get_settings
from aggrigator.db import session_scope
from aggrigator.ingest.client import SgoClient
from aggrigator.ingest.orchestrator import ingest_due_leagues
from aggrigator.ingest.quota import quota_status
from aggrigator.ingest.sgo_http import SgoHttpClient
from aggrigator.ingest.sgo_simulator import FixtureSgoClient
from aggrigator.ops.progress import set_progress

logger = logging.getLogger(__name__)


def _build_client() -> SgoClient:
    """Pick a client per env. Fixture mode wins if ``SPORTSGAMEODDS_FIXTURE_DIR``
    is set — useful in dev without burning the SGO quota.

    The HTTP client gets the configured rate-limit knobs so seed +
    full_refresh self-throttle under SGO's 10/min cap instead of failing
    the whole cron on the first 429.
    """
    settings = get_settings()
    if settings.sgo_fixture_path is not None:
        return FixtureSgoClient(settings.sgo_fixture_path)
    return SgoHttpClient(
        base_url=settings.sgo_base_url,
        api_key=settings.sgo_api_key,
        min_interval=settings.sgo_min_interval_seconds,
        max_retries=settings.sgo_max_retries,
    )


async def _run_ingest(phase: str, cron_name: str) -> dict[str, Any]:
    """Common body for the three ingest phases. ``phase`` forwards to
    ``ingest_due_leagues`` / ``ingest_event``; ``cron_name`` shows up in
    the log lines so the deploy-log breadcrumbs match what's on /ops/crons.
    """
    logger.info("%s: starting", cron_name)
    await set_progress(f"{phase}: checking SGO monthly quota")
    settings = get_settings()
    client = _build_client()
    qs = quota_status(
        client,
        threshold_pct=settings.sgo_quota_threshold_pct,
        reset_day=settings.sgo_quota_reset_day,
        pace_floor_pct=settings.sgo_quota_pace_floor_pct,
    )
    # Surface the live numbers so operators can see "we're at 87% on
    # entities" in the SSE log without grepping deploy logs or running
    # scripts/check_quota.py. Emit each line separately so the log
    # shows them stacked instead of as one long string.
    for line in qs.summary_lines:
        await set_progress(line)
    if not settings.test_mode and qs.should_skip:
        # Absolute guard tripped → already over threshold, can't run.
        # Pace guard tripped → projected to overshoot by reset day,
        # back off auto runs to leave headroom for manual ops.
        reason = "sgo_monthly_quota" if qs.exhausted else "sgo_quota_pace"
        logger.warning(
            "%s: skipped — quota guard tripped (reason=%s). "
            "Bump AGG_SGO_QUOTA_THRESHOLD_PCT, AGG_SGO_QUOTA_PACE_FLOOR_PCT, "
            "or wait for reset.",
            cron_name, reason,
        )
        await set_progress(
            f"{phase}: SKIPPED — {qs.pace_reason if not qs.exhausted else 'absolute cap reached'}"
        )
        return {
            "skipped": True,
            "reason": reason,
            "phase": phase,
            "pace_reason": qs.pace_reason,
        }
    await set_progress(f"{phase}: walking active leagues")
    async with session_scope() as session:
        reports = await ingest_due_leagues(session, client, phase=phase)
    summary = {
        "phase": phase,
        "leagues_walked": len(reports),
        "events_processed": sum(r.events_processed for r in reports),
        "events_failed": sum(r.events_failed for r in reports),
    }
    logger.info("%s: complete %s", cron_name, summary)
    return summary


async def run_ingest_due_leagues() -> dict[str, Any]:
    """Walk every active League once with the FULL pipeline (events +
    markets + selections + bookmaker quotes + lifecycle + webhooks).

    Used by ``full_refresh`` and the standalone scheduled cron. Heavy —
    if the per-league walk is consistently slow, prefer running
    ``ingest_event_lifecycle`` frequently and ``ingest_event_odds``
    less often (cheaper per run, same total work).
    """
    return await _run_ingest("all", "ingest_due_leagues")


async def run_ingest_lifecycle_only() -> dict[str, Any]:
    """Lightweight variant: event row + status + settle + webhooks.

    Skips the bookmaker-quote upserts (the dominant per-event cost). Use
    when you need score / lifecycle freshness without per-bookmaker price
    refreshes (e.g. live game tracking).
    """
    return await _run_ingest("lifecycle", "ingest_event_lifecycle")


async def run_ingest_odds_only() -> dict[str, Any]:
    """Counterpart to lifecycle: refreshes markets + per-bookmaker prices
    on events that already exist in our DB.

    New events not yet in the DB are skipped — run lifecycle (or a full
    ingest) first to create their rows. No webhooks fire from this
    phase; transitions happen exclusively in the lifecycle pass.
    """
    return await _run_ingest("odds", "ingest_event_odds")


# ---- ARQ entry points ------------------------------------------------------


async def ingest_due_leagues_task(ctx: dict) -> dict:
    """ARQ-callable wrapper. Wrapped by ``cron_run_recorder`` so every
    scheduled run lands in the ``cron_run`` table (plan §2.1.4)."""
    from aggrigator.ops.recorder import cron_run_recorder

    @cron_run_recorder("ingest_due_leagues")
    async def _runner(ctx_):
        return await run_ingest_due_leagues()

    return await _runner(ctx)


async def ingest_lifecycle_only_task(ctx: dict) -> dict:
    from aggrigator.ops.recorder import cron_run_recorder

    @cron_run_recorder("ingest_event_lifecycle")
    async def _runner(ctx_):
        return await run_ingest_lifecycle_only()

    return await _runner(ctx)


async def ingest_odds_only_task(ctx: dict) -> dict:
    from aggrigator.ops.recorder import cron_run_recorder

    @cron_run_recorder("ingest_event_odds")
    async def _runner(ctx_):
        return await run_ingest_odds_only()

    return await _runner(ctx)
