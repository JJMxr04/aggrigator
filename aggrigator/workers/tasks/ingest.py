"""Ingest task — runnable both as an ARQ job and as a plain async function
(so tests can call it without a Redis dependency)."""

from __future__ import annotations

import logging
from typing import Any

from aggrigator.config import get_settings
from aggrigator.db import session_scope
from aggrigator.ingest.client import SgoClient
from aggrigator.ingest.odds_api_http import OddsApiHttpClient
from aggrigator.ingest.odds_api_quota import quota_status as oddsapi_quota_status
from aggrigator.ingest.orchestrator import ingest_due_leagues
from aggrigator.ingest.quota import quota_status as sgo_quota_status
from aggrigator.ingest.sgo_http import SgoHttpClient
from aggrigator.ingest.sgo_simulator import FixtureSgoClient
from aggrigator.ops.progress import set_progress

logger = logging.getLogger(__name__)


def _build_client() -> SgoClient:
    """Pick a client per env. Fixture mode wins for SGO if
    ``SPORTSGAMEODDS_FIXTURE_DIR`` is set; otherwise ``AGG_ODDS_PROVIDER``
    selects between SGO HTTP and odds-api.io HTTP.

    The HTTP client gets the configured rate-limit knobs so seed +
    full_refresh self-throttle under each provider's cap instead of
    failing the whole cron on the first 429.
    """
    settings = get_settings()
    if settings.sgo_fixture_path is not None:
        # Fixture mode (dev) — always SGO shape regardless of provider flag.
        return FixtureSgoClient(settings.sgo_fixture_path)
    if settings.odds_provider == "oddsapi":
        return OddsApiHttpClient(
            base_url=settings.odds_api_base_url,
            api_key=settings.odds_api_key,
        )
    return SgoHttpClient(
        base_url=settings.sgo_base_url,
        api_key=settings.sgo_api_key,
        min_interval=settings.sgo_min_interval_seconds,
        max_retries=settings.sgo_max_retries,
    )


async def run_ingest_due_leagues() -> dict[str, Any]:
    """Walk every active League once with the full pipeline (events +
    markets + selections + bookmaker quotes + lifecycle + webhooks).

    This is the auto-scheduled cron and also what ``full_refresh`` calls
    after seeding. The lifecycle-only / odds-only split variants were
    removed when the cron registry was trimmed — the combined walk is
    always the cost-efficient default on both providers.
    """
    settings = get_settings()
    is_oddsapi = (
        settings.odds_provider == "oddsapi"
        and settings.sgo_fixture_path is None
    )
    quota_label = "odds-api per-hour" if is_oddsapi else "SGO monthly"
    logger.info("ingest_due_leagues: starting")
    await set_progress(f"checking {quota_label} quota")
    client = _build_client()
    if is_oddsapi:
        qs = oddsapi_quota_status(
            client, threshold_pct=settings.odds_api_throttle_pct,
        )
    else:
        qs = sgo_quota_status(
            client,
            threshold_pct=settings.sgo_quota_threshold_pct,
            reset_day=settings.sgo_quota_reset_day,
            pace_floor_pct=settings.sgo_quota_pace_floor_pct,
        )
    # Surface each quota line as its own progress entry so /ops/crons
    # shows the stack instead of one mega-string.
    for line in qs.summary_lines:
        await set_progress(line)
    if not settings.test_mode and qs.should_skip:
        if is_oddsapi:
            reason = "oddsapi_per_hour_quota"
        else:
            reason = "sgo_monthly_quota" if qs.exhausted else "sgo_quota_pace"
        logger.warning(
            "ingest_due_leagues: skipped — quota guard tripped (reason=%s). "
            "Bump the relevant threshold env var or wait for reset.",
            reason,
        )
        await set_progress(
            "SKIPPED — "
            f"{qs.pace_reason if not qs.exhausted else 'absolute cap reached'}"
        )
        return {
            "skipped": True,
            "reason": reason,
            "pace_reason": qs.pace_reason,
        }
    await set_progress("walking active leagues")
    async with session_scope() as session:
        reports = await ingest_due_leagues(session, client)
    summary: dict[str, Any] = {
        "leagues_walked": len(reports),
        "events_processed": sum(r.events_processed for r in reports),
        "events_failed": sum(r.events_failed for r in reports),
    }
    # Provider-specific telemetry (best-practices.md §8). Lands in
    # cron_run.summary; surfaces in /ops/crons. p50/p95 are computed
    # client-side from per-request durations.
    if is_oddsapi:
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


# ---- ARQ entry point -------------------------------------------------------


async def ingest_due_leagues_task(ctx: dict) -> dict:
    """ARQ-callable wrapper. Wrapped by ``cron_run_recorder`` so every
    scheduled run lands in the ``cron_run`` table."""
    from aggrigator.ops.recorder import cron_run_recorder

    @cron_run_recorder("ingest_due_leagues")
    async def _runner(ctx_):
        return await run_ingest_due_leagues()

    return await _runner(ctx)
