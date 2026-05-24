"""``load_registry`` worker task.

Reads the JSON registry at ``aggrigator/data/sports/``, validates, and
applies it to the DB. Recomputes ``League.can_pull_historical_scores``
on each run.

Surfaced as a manual-only cron in ``aggrigator.ops.registry`` so operators
can re-run it from the ``/ops/crons`` UI after editing the JSON files.
"""

from __future__ import annotations

import logging

from aggrigator.db import session_scope
from aggrigator.ingest.registry import (
    apply_registry_to_db,
    load_registry_from_disk,
    report_as_dict,
    validate_registry,
)
from aggrigator.ops.recorder import cron_run_recorder
from aggrigator.workers.app import app

logger = logging.getLogger(__name__)


async def run_load_registry() -> dict:
    """Validate + apply the on-disk registry. Returns the apply report.

    Raises ``RuntimeError`` on validation failure so the cron run is
    recorded as FAILED with the issue list — operators should fix the
    JSON before retrying.
    """
    logger.info("load_registry: starting")
    snap = load_registry_from_disk()
    issues = validate_registry(snap)
    if issues:
        joined = "\n  - " + "\n  - ".join(issues)
        raise RuntimeError(f"registry validation failed:{joined}")

    async with session_scope() as session:
        report = await apply_registry_to_db(session, snap)

    result = report_as_dict(report)
    logger.info("load_registry: complete %s", result)
    return result


# ---- Procrastinate entry point --------------------------------------------


# Manual-only — no ``@app.periodic``. Operators trigger it from /ops/crons
# after editing the on-disk registry JSON.
@app.task(
    name="aggrigator.load_registry",
    pass_context=True,
    queueing_lock="load_registry",
)
@cron_run_recorder("load_registry")
async def load_registry_task(context) -> dict:
    return await run_load_registry()
