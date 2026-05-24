"""``cron_run`` write helpers — used by both the Procrastinate worker and the
manual trigger path so every execution lands in the same table.

Two entry points:

- ``run_with_recording(spec, *, trigger_source, started_by_user_id)``: full
  shape — opens a cron_run row, calls ``spec.runner()``, marks success/failure,
  truncates errors, returns the resulting CronRun row.

- ``cron_run_recorder(spec_name)``: decorator wrapped around each Procrastinate
  task body so scheduled executions write the same kind of row as manual
  triggers. The wrapper looks up the spec from the registry and delegates to
  ``run_with_recording`` with ``trigger_source='scheduled'``.

Retry policy lives on the task itself via ``@app.task(retry=RetryStrategy(...))``
— this decorator just re-raises retryable exceptions so Procrastinate's
strategy decides whether to schedule another attempt. That keeps the retry
math next to the task declaration where someone editing the cadence will see
it, instead of behind a decorator wrapper.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.db import session_scope
from aggrigator.models import CronRun, CronRunSource, CronRunStatus, CronSchedule

if False:  # TYPE_CHECKING guard — avoid the circular import at runtime
    from aggrigator.ops.registry import CronSpec

logger = logging.getLogger(__name__)

ERROR_TRUNCATE_CHARS = 4000


async def _scheduled_run_enabled(cron_name: str) -> bool:
    """Read cron_schedule.enabled for a scheduled cron tick.

    Defaults to True when no row exists (first deploy after the
    migration). On any DB error we also default to True — a Postgres
    blip should not silently stop the scheduler; the scheduled job will
    run, fail loudly through the normal recorder path if there's a real
    DB outage, and the operator will see it in /ops/crons.
    """
    try:
        async with session_scope() as session:
            row = await session.get(CronSchedule, cron_name)
            return True if row is None else row.enabled
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cron %s: could not read cron_schedule.enabled (%s: %s) — "
            "assuming enabled and proceeding",
            cron_name, type(exc).__name__, exc,
        )
        return True


async def open_run(
    session: AsyncSession,
    spec: "CronSpec",
    *,
    trigger_source: str,
    started_by_user_id: uuid.UUID | None,
    job_id: str,
) -> CronRun:
    row = CronRun(
        cron_name=spec.name,
        trigger_source=trigger_source,
        started_by_user_id=started_by_user_id,
        job_id=job_id,
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
    spec: "CronSpec",
    *,
    trigger_source: str,
    started_by_user_id: uuid.UUID | None = None,
    job_id: str | None = None,
) -> tuple[CronRun, dict | None, BaseException | None]:
    """Runs ``spec.runner()`` and writes a ``cron_run`` row on either path."""
    jid = job_id or str(uuid.uuid4())
    summary: dict | None = None
    error: BaseException | None = None

    async with session_scope() as session:
        row = await open_run(
            session, spec,
            trigger_source=trigger_source,
            started_by_user_id=started_by_user_id,
            job_id=jid,
        )
        await session.commit()
        run_id = row.id

    try:
        summary = await spec.runner()
    except BaseException as exc:  # noqa: BLE001 — we re-raise after recording
        error = exc

    async with session_scope() as session:
        row = await session.get(CronRun, run_id)
        await close_run(session, row, summary=summary, error=error)

    if error is not None:
        logger.error("cron run failed: cron=%s run_id=%s", spec.name, run_id)
        # Re-raise so Procrastinate marks the job failed too (and its retry
        # strategy decides whether to schedule another attempt). Manual-trigger
        # callers catch this themselves to surface the error in the API.
        raise error

    logger.info("cron run ok: cron=%s run_id=%s", spec.name, run_id)
    return row, summary, error


def cron_run_recorder(
    spec_name: str,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Wrap a Procrastinate task body in cron_run recording + the pause gate.

    Scheduled runs hit this; manual triggers go through
    ``CronService.trigger`` which calls ``run_with_recording`` directly
    and bypasses this wrapper (manual Run-now still works on paused
    crons by design).

    Retry math lives in the ``@app.task(retry=RetryStrategy(...))`` decorator
    on the task itself — Procrastinate consults the strategy on raise. This
    wrapper just records the attempt and re-raises so the strategy fires.

    Usage::

        @app.periodic(cron="30 2 * * *")
        @app.task(name="aggrigator.ingest_due_leagues",
                  pass_context=True,
                  queueing_lock="ingest_due_leagues",
                  retry=RetryStrategy(max_attempts=2, wait=300,
                                       retry_exceptions=(asyncio.TimeoutError,
                                                          RateLimitExceededError)))
        @cron_run_recorder("ingest_due_leagues")
        async def ingest_due_leagues_task(context):
            ...
    """
    def _decorate(fn: Callable[..., Awaitable[Any]]):
        @wraps(fn)
        async def _wrapped(*args, **kwargs):
            # Lazy imports — the registry imports the task modules to build
            # CronSpec entries, which would cycle if we resolved at module
            # import / decorator-apply time. By call time the cycle is
            # closed (every module fully loaded) and the lookup is cheap.
            from aggrigator.ops.registry import by_name
            from aggrigator.ops.service import _advisory_key

            spec = by_name(spec_name)
            if spec is None:
                raise ValueError(f"Unknown cron spec: {spec_name}")
            # Pause gate (cron_schedule.enabled, /ops/crons toggle). If an
            # operator has flipped the cron off, skip the run entirely —
            # no cron_run row, no provider calls. A disabled cron should
            # be silent, not write a no-op record every tick. Manual
            # triggers bypass this wrapper, so Run-now still works.
            if not await _scheduled_run_enabled(spec_name):
                logger.info(
                    "cron %s: paused via /ops/crons — scheduled tick skipped",
                    spec_name,
                )
                return {"skipped": True, "reason": "disabled"}

            # Procrastinate hands the task body a ``context`` keyword arg
            # when the task is decorated with ``pass_context=True``. The
            # job's row ID is unique per attempt, which is exactly what
            # the cron_run.job_id unique index wants.
            context = kwargs.get("context") or (args[0] if args else None)
            base_id = getattr(getattr(context, "job", None), "id", None)
            job_id = f"procrastinate-{base_id}" if base_id else str(uuid.uuid4())

            # Mutex with the manual-trigger path (CronService.trigger).
            # Manual runs execute inline in the FastAPI worker and hold
            # this advisory lock for the duration; if the scheduler fires
            # while one is in flight, we skip rather than racing the
            # inline run. The lock is released when ``lock_session`` exits.
            lock_key = _advisory_key(spec_name)
            async with session_scope() as lock_session:
                got = await lock_session.scalar(
                    text("SELECT pg_try_advisory_lock(:k)"),
                    {"k": lock_key},
                )
                if not got:
                    logger.info(
                        "cron %s: scheduled tick skipped — a manual run "
                        "is in flight (advisory lock held)",
                        spec_name,
                    )
                    return {"skipped": True, "reason": "manual_in_flight"}
                try:
                    _, summary, _ = await run_with_recording(
                        spec,
                        trigger_source=CronRunSource.SCHEDULED,
                        job_id=job_id,
                    )
                finally:
                    await lock_session.execute(
                        text("SELECT pg_advisory_unlock(:k)"),
                        {"k": lock_key},
                    )
            return summary

        return _wrapped

    return _decorate
