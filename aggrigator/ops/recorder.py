"""``cron_run`` write helpers — used by both ARQ scheduled runs and manual
trigger paths so every execution lands in the same table.

Two entry points:

- ``run_with_recording(spec, *, trigger_source, started_by_user_id)``: full
  shape — opens a cron_run row, calls ``spec.runner()``, marks success/failure,
  truncates errors, returns the resulting CronRun row.

- ``cron_run_recorder(spec_name)``: ARQ-task decorator that wraps a scheduled
  task body in the same machinery. The wrapper looks up the spec from the
  registry and delegates to ``run_with_recording`` with
  ``trigger_source='scheduled'``.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.db import session_scope
from aggrigator.models import CronRun, CronRunSource, CronRunStatus
from aggrigator.ops.progress import (
    CronCancelled,
    clear_cancel,
    clear_progress,
    current_run_id,
    publish_complete,
)
from aggrigator.ops.registry import CronSpec, by_name

logger = logging.getLogger(__name__)

ERROR_TRUNCATE_CHARS = 4000


async def open_run(
    session: AsyncSession,
    spec: CronSpec,
    *,
    trigger_source: str,
    started_by_user_id: uuid.UUID | None,
    arq_job_id: str,
) -> CronRun:
    row = CronRun(
        cron_name=spec.name,
        trigger_source=trigger_source,
        started_by_user_id=started_by_user_id,
        arq_job_id=arq_job_id,
        status=CronRunStatus.RUNNING,
    )
    session.add(row)
    await session.flush()
    return row


async def close_run(
    session: AsyncSession,
    row: CronRun,
    *,
    summary: dict | None,
    error: BaseException | None,
) -> None:
    row.finished_at = datetime.now(tz=timezone.utc)
    if error is not None:
        if isinstance(error, CronCancelled):
            # Operator clicked Stop. Distinct status, no traceback —
            # cancellation isn't an error condition.
            row.status = CronRunStatus.CANCELLED
            row.error = str(error)[:ERROR_TRUNCATE_CHARS]
        else:
            row.status = CronRunStatus.FAILED
            # truncate at write time (plan §2.1.8)
            row.error = (
                f"{type(error).__name__}: {error}\n\n"
                + "".join(traceback.format_exception(error))
            )[:ERROR_TRUNCATE_CHARS]
    else:
        row.status = CronRunStatus.SUCCESS
        row.summary = summary
        # Best-effort items_processed extraction. Each cron's summary has its
        # own shape; we look for common keys without enforcing a schema.
        if isinstance(summary, dict):
            for key in ("events_processed", "sent", "selections_settled", "leagues"):
                if key in summary and isinstance(summary[key], int):
                    row.items_processed = summary[key]
                    break


async def run_with_recording(
    spec: CronSpec,
    *,
    trigger_source: str,
    started_by_user_id: uuid.UUID | None = None,
    arq_job_id: str | None = None,
) -> tuple[CronRun, dict | None, BaseException | None]:
    """Runs ``spec.runner()`` and writes a ``cron_run`` row on either path.

    The caller is responsible for the Redis lock (§2.1.6) — this function
    deliberately does NOT acquire it, because both the manual-trigger path
    and the ARQ scheduled path need it but acquire it at different layers.
    """
    aji = arq_job_id or str(uuid.uuid4())
    summary: dict | None = None
    error: BaseException | None = None

    async with session_scope() as session:
        row = await open_run(
            session, spec,
            trigger_source=trigger_source,
            started_by_user_id=started_by_user_id,
            arq_job_id=aji,
        )
        await session.commit()
        run_id = row.id

    # Make run_id ambient so ``ops.progress.set_progress`` calls inside
    # the runner know where to write. Reset on the way out so progress
    # writes from outside a cron context (manual shell, tests) are no-ops.
    progress_token = current_run_id.set(run_id)
    try:
        summary = await spec.runner()
    except BaseException as exc:  # noqa: BLE001 — we re-raise after recording
        error = exc
    finally:
        current_run_id.reset(progress_token)
        # Tell live SSE subscribers the stream is done so their
        # EventSource closes cleanly. Publish *before* clearing the
        # backlog so any client currently reading the channel gets
        # the sentinel — a deleted key with no publisher would just
        # leave the connection hanging until the proxy timed it out.
        await publish_complete(run_id)
        # Best-effort cleanup. TTL on both keys would handle this
        # eventually, but the UI re-renders within 2s and we want
        # subsequent runs to have a clean slate.
        await clear_progress(run_id)
        await clear_cancel(run_id)

    async with session_scope() as session:
        row = await session.get(CronRun, run_id)
        await close_run(session, row, summary=summary, error=error)

    if error is not None:
        logger.error("cron run failed: cron=%s run_id=%s", spec.name, run_id)
        # Re-raise so ARQ marks the job failed too. Manual-trigger callers
        # catch this themselves to surface the error in the API response.
        raise error

    logger.info("cron run ok: cron=%s run_id=%s", spec.name, run_id)
    return row, summary, error


def cron_run_recorder(spec_name: str) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Decorator for ARQ task bodies — wraps them with cron_run recording.

    Usage::

        @cron_run_recorder("ingest_due_leagues")
        async def ingest_due_leagues_task(ctx):
            ...
    """
    spec = by_name(spec_name)
    if spec is None:
        raise ValueError(f"Unknown cron spec: {spec_name}")

    def _decorate(fn: Callable[..., Awaitable[Any]]):
        @wraps(fn)
        async def _wrapped(ctx: dict, *args, **kwargs):
            arq_job_id = (ctx or {}).get("job_id") or str(uuid.uuid4())
            _, summary, _ = await run_with_recording(
                spec,
                trigger_source=CronRunSource.SCHEDULED,
                arq_job_id=arq_job_id,
            )
            return summary

        return _wrapped

    return _decorate
