"""Ingest tasks — runnable both as ARQ jobs and as plain async functions
(so tests can call them without a Redis dependency)."""

from __future__ import annotations

import logging
from typing import Any

from aggrigator.config import get_settings
from aggrigator.db import session_scope
from aggrigator.ingest.client import SgoClient
from aggrigator.ingest.orchestrator import ingest_due_leagues
from aggrigator.ingest.quota import is_monthly_quota_exhausted
from aggrigator.ingest.sgo_http import SgoHttpClient
from aggrigator.ingest.sgo_simulator import FixtureSgoClient

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


async def run_ingest_due_leagues() -> dict[str, Any]:
    """Walk every active League once, ingest, return summary report.

    The body is plain async so tests can call it with their own session
    (they construct the client themselves and call ``ingest_due_leagues``
    directly). ARQ wraps this with a session_scope.
    """
    settings = get_settings()
    client = _build_client()
    # Pre-flight monthly quota check — skip the run if SGO usage is past
    # AGG_SGO_QUOTA_THRESHOLD_PCT. test_mode bypasses this so dev / CI
    # never short-circuit on synthetic usage payloads.
    if not settings.test_mode and is_monthly_quota_exhausted(
        client, threshold_pct=settings.sgo_quota_threshold_pct,
    ):
        return {"skipped": True, "reason": "sgo_monthly_quota"}
    async with session_scope() as session:
        reports = await ingest_due_leagues(session, client)
    summary = {
        "leagues_walked": len(reports),
        "events_processed": sum(r.events_processed for r in reports),
        "events_failed": sum(r.events_failed for r in reports),
    }
    logger.info("ingest_due_leagues report: %s", summary)
    return summary


# ---- ARQ entry points ------------------------------------------------------


async def ingest_due_leagues_task(ctx: dict) -> dict:
    """ARQ-callable wrapper. Wrapped by ``cron_run_recorder`` so every
    scheduled run lands in the ``cron_run`` table (plan §2.1.4)."""
    from aggrigator.ops.recorder import cron_run_recorder

    @cron_run_recorder("ingest_due_leagues")
    async def _runner(ctx_):
        return await run_ingest_due_leagues()

    return await _runner(ctx)
