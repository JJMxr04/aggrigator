"""Ingest tasks — runnable both as ARQ jobs and as plain async functions
(so tests can call them without a Redis dependency)."""

from __future__ import annotations

import logging
from typing import Any

from aggrigator.config import get_settings
from aggrigator.db import session_scope
from aggrigator.ingest.client import SgoClient
from aggrigator.ingest.orchestrator import ingest_due_leagues
from aggrigator.ingest.sgo_http import SgoHttpClient
from aggrigator.ingest.sgo_simulator import FixtureSgoClient

logger = logging.getLogger(__name__)


def _build_client() -> SgoClient:
    """Pick a client per env. Fixture mode wins if ``SPORTSGAMEODDS_FIXTURE_DIR``
    is set — useful in dev without burning the SGO quota."""
    settings = get_settings()
    if settings.sgo_fixture_path is not None:
        return FixtureSgoClient(settings.sgo_fixture_path)
    return SgoHttpClient(base_url=settings.sgo_base_url, api_key=settings.sgo_api_key)


async def run_ingest_due_leagues() -> dict[str, Any]:
    """Walk every active League once, ingest, return summary report.

    The body is plain async so tests can call it with their own session
    (they construct the client themselves and call ``ingest_due_leagues``
    directly). ARQ wraps this with a session_scope.
    """
    client = _build_client()
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
    """ARQ-callable wrapper."""
    return await run_ingest_due_leagues()
