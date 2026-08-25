"""Ingest task — runnable both as a Procrastinate job and as a plain async
function (so tests can call it without a worker / Postgres queue dependency)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from procrastinate import RetryStrategy

from aggrigator.config import get_settings
from aggrigator.db import session_scope
from aggrigator.ingest.client import OddsClient
from aggrigator.ingest.odds_api_errors import RateLimitExceededError
from aggrigator.ingest.odds_api_http import OddsApiHttpClient
from aggrigator.ingest.odds_api_quota import quota_status
from aggrigator.ingest.orchestrator import ingest_due_leagues
from aggrigator.ops.recorder import cron_run_recorder
from aggrigator.workers.app import app

logger = logging.getLogger(__name__)


# Retry policy shared by the discovery + refresh walks. Identical failure
# modes (network-bound, same orchestrator path, per-league commits make
# the retry naturally pick up where the original left off). ``wait=300``
# = five minutes — long enough for an upstream blip to clear, short enough
# that the next scheduled tick won't beat us to it on hourly crons.
_INGEST_RETRY = RetryStrategy(
    max_attempts=2,
    wait=300,
    retry_exceptions=(asyncio.TimeoutError, RateLimitExceededError),
)


def _build_client() -> OddsClient:
    """Build the odds-api.io HTTP client with configured retry budget.
    Base URL is hardcoded as the client kwarg default."""
    settings = get_settings()
    return OddsApiHttpClient(
        api_key=settings.odds_api_key,
        max_retries=settings.odds_api_max_retries,
    )


async def run_ingest_due_leagues(
    *, discover_new_events: bool = True,
) -> dict[str, Any]:
    """Walk every active League once with the full pipeline (events +
    markets + selections + bookmaker quotes + lifecycle + webhooks).

    Auto-scheduled twice with different intent:

    - **Discovery walk** (``discover_new_events=True``, daily): inserts
      newly surfaced events plus refreshes existing ones. Same call shape
      ``full_refresh`` invokes after seeding.
    - **Refresh walk** (``discover_new_events=False``, intra-day): only
      touches events already in DB. New events deferred to the daily
      discovery cron — keeps the hourly cycle's per-event write cost
      bounded by the existing event count.
    """
    settings = get_settings()
    logger.info("ingest_due_leagues: starting — checking odds-api per-hour quota")
    client = _build_client()
    qs = quota_status(client, threshold_pct=settings.odds_api_throttle_pct)
    for line in qs.summary_lines:
        logger.info("ingest_due_leagues: quota — %s", line)
    if not settings.test_mode and qs.should_skip:
        logger.warning(
            "ingest_due_leagues: SKIPPED — quota guard tripped (%s). "
            "Bump AGG_ODDSAPI_THROTTLE_PCT or wait for the per-hour bucket reset.",
            qs.pace_reason,
        )
        return {
            "skipped": True,
            "reason": "oddsapi_per_hour_quota",
            "pace_reason": qs.pace_reason,
        }
    mode = "discovery" if discover_new_events else "refresh-only"
    logger.info("ingest_due_leagues: walking active leagues (%s)", mode)
    async with session_scope() as session:
        reports = await ingest_due_leagues(
            session, client,
            discover_new_events=discover_new_events,
        )
    summary: dict[str, Any] = {
        "mode": mode,
        "leagues_walked": len(reports),
        "events_processed": sum(r.events_processed for r in reports),
        "events_failed": sum(r.events_failed for r in reports),
    }
    # Per-cycle telemetry. Lands in
    # cron_run.summary; surfaces in /ops/crons. p50/p95 are computed
    # client-side from per-request durations.
    durs = sorted(client.request_durations_ms)
    n = len(durs)
    summary.update({
        "oddsapi_request_count": client.request_count,
        "oddsapi_error_count": client.error_count,
        "oddsapi_429_count": client.rate_limit_hits,
        "oddsapi_p50_ms": durs[n // 2] if n else 0,
        "oddsapi_p95_ms": durs[int(n * 0.95)] if n else 0,
        "oddsapi_ratelimit_limit": client.last_ratelimit_limit,
        "oddsapi_ratelimit_remaining_min": client.last_ratelimit_remaining,
    })
    logger.info("ingest_due_leagues: complete %s", summary)
    return summary


# ---- Procrastinate entry points -------------------------------------------


async def run_refresh_existing_events() -> dict[str, Any]:
    """Convenience wrapper for the registry/UI runner contract — same
    pipeline as ``run_ingest_due_leagues`` with new-event discovery off."""
    return await run_ingest_due_leagues(discover_new_events=False)


# Daily DISCOVERY walk: inserts newly surfaced events + refreshes existing ones.
@app.periodic(cron="30 2 * * *")
@app.task(
    name="aggrigator.ingest_due_leagues",
    pass_context=True,
    queueing_lock="ingest_due_leagues",
    retry=_INGEST_RETRY,
)
@cron_run_recorder("ingest_due_leagues")
async def ingest_due_leagues_task(context, timestamp: int | None = None) -> dict:
    return await run_ingest_due_leagues(discover_new_events=True)


# REFRESH-only walk for events already in DB. Hourly @ :00 except hour 2,
# which is the daily discovery slot — running both at once would race on
# the same league.
@app.periodic(cron="0 0,1,3-23 * * *")
@app.task(
    name="aggrigator.refresh_existing_events",
    pass_context=True,
    queueing_lock="refresh_existing_events",
    retry=_INGEST_RETRY,
)
@cron_run_recorder("refresh_existing_events")
async def refresh_existing_events_task(context, timestamp: int | None = None) -> dict:
    return await run_ingest_due_leagues(discover_new_events=False)
