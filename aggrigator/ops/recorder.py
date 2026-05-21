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

import asyncio
import logging
import traceback
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Awaitable, Callable

import redis.exceptions as _redis_exc
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.db import session_scope
from aggrigator.ingest.odds_api_errors import RateLimitExceededError
from aggrigator.models import CronRun, CronRunSource, CronRunStatus
from aggrigator.ops.registry import CronSpec, by_name

logger = logging.getLogger(__name__)

ERROR_TRUNCATE_CHARS = 4000

# Errors a retry might actually fix. Anything outside this set
# (ValidationError, InvalidApiKeyError, programming bugs, etc.) is
# terminal — retrying just burns API quota and worker slots.
#
# - asyncio.TimeoutError: arq's job_timeout fired or upstream stalled.
# - RateLimitExceededError: per-hour bucket exhausted; defers past reset.
# - redis.RedisError + ConnectionError: transient infra hiccup on the
#   arq queue / lock path.
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    RateLimitExceededError,
    _redis_exc.RedisError,
    _redis_exc.ConnectionError,
)
# Default deferral for an auto-retry. Five minutes is long enough for a
# Redis blip to clear and short enough that the next scheduled tick
# doesn't beat us to it on hourly crons.
DEFAULT_RETRY_DEFER_SECONDS = 300


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

    try:
        summary = await spec.runner()
    except BaseException as exc:  # noqa: BLE001 — we re-raise after recording
        error = exc

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


def cron_run_recorder(
    spec_name: str,
    *,
    retryable: bool = False,
    retry_defer_seconds: int = DEFAULT_RETRY_DEFER_SECONDS,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Decorator for ARQ task bodies — wraps them with cron_run recording.

    When ``retryable=True``, transient failures (see ``RETRYABLE_EXCEPTIONS``)
    are converted into ``arq.worker.Retry(defer=retry_defer_seconds)`` IF the
    cron's ``max_tries`` budget allows another attempt. The cron_run row is
    still written by ``run_with_recording`` so the failure is visible in
    /ops/crons; arq enqueues a fresh job for the retry. Per-league commits
    in the orchestrator mean the retry naturally picks up where the
    original left off (already-finished leagues are cheap UPSERTs).

    Terminal failures (ValidationError, InvalidApiKeyError, bugs) and
    arq.CronJobs without a retry budget propagate unchanged — re-raised so
    arq marks the job failed.

    Usage::

        @cron_run_recorder("ingest_due_leagues", retryable=True)
        async def ingest_due_leagues_task(ctx):
            ...
    """
    spec = by_name(spec_name)
    if spec is None:
        raise ValueError(f"Unknown cron spec: {spec_name}")

    def _decorate(fn: Callable[..., Awaitable[Any]]):
        @wraps(fn)
        async def _wrapped(ctx: dict, *args, **kwargs):
            ctx = ctx or {}
            # arq's ctx["job_id"] is the queue-time id — fine for tracing,
            # but NOT unique per execution when callers use a fixed
            # coalescing `_job_id` (e.g. webhook_deliver:due, which the
            # orchestrator + watchdog reuse so concurrent push-on-write
            # calls dedupe into one queued job). The cron_run unique
            # index on arq_job_id then rejects the second firing's
            # INSERT. Suffix a short uuid token so each execution gets
            # its own arq_job_id while keeping the original id as a
            # searchable prefix in the admin / /ops UI.
            base_id = ctx.get("job_id")
            if base_id:
                # Reserve 9 chars for "#xxxxxxxx" so the suffix is never
                # truncated away — uniqueness is what matters.
                max_base = 64 - 9
                arq_job_id = f"{base_id[:max_base]}#{uuid.uuid4().hex[:8]}"
            else:
                arq_job_id = str(uuid.uuid4())
            try:
                _, summary, _ = await run_with_recording(
                    spec,
                    trigger_source=CronRunSource.SCHEDULED,
                    arq_job_id=arq_job_id,
                )
            except RETRYABLE_EXCEPTIONS as exc:
                if not retryable:
                    raise
                # ctx["job_try"] is 1-indexed; ctx["max_tries"] is the
                # budget arq passed in (set per-cron in workers/settings.py).
                # If neither is present we're outside an arq context (test
                # harness, manual call) so just re-raise.
                job_try = ctx.get("job_try")
                max_tries = ctx.get("max_tries")
                if job_try is None or max_tries is None:
                    raise
                if job_try >= max_tries:
                    logger.warning(
                        "cron %s: transient failure on final try %d/%d — "
                        "giving up, will wait for next scheduled tick (%s)",
                        spec_name, job_try, max_tries, type(exc).__name__,
                    )
                    raise
                from arq.worker import Retry  # local import — keeps cold-start light
                logger.warning(
                    "cron %s: transient failure on try %d/%d — deferring "
                    "%ds for retry (%s: %s)",
                    spec_name, job_try, max_tries, retry_defer_seconds,
                    type(exc).__name__, exc,
                )
                raise Retry(defer=retry_defer_seconds)
            return summary

        return _wrapped

    return _decorate
