"""Unit tests for ``cron_run_recorder`` retry classification.

The recorder doesn't know how arq dispatches retries — it just translates
transient failures into ``arq.worker.Retry`` exceptions when the cron's
budget allows. These tests stub out ``run_with_recording`` so we can drive
exception paths without spinning up Postgres/Redis.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from arq.worker import Retry

from aggrigator.ingest.odds_api_errors import RateLimitExceededError, ValidationError
from aggrigator.ops.recorder import cron_run_recorder


def _ctx(*, job_try: int = 1, max_tries: int = 2) -> dict:
    return {"job_id": "test-job", "job_try": job_try, "max_tries": max_tries}


@pytest.fixture(autouse=True)
def _allow_scheduled_run():
    """Skip the cron_schedule.enabled DB lookup — these tests stub the
    recorder upstream of the actual SQL paths and don't run migrations."""
    with patch(
        "aggrigator.ops.recorder._scheduled_run_enabled",
        new=AsyncMock(return_value=True),
    ):
        yield


@pytest.mark.asyncio
async def test_retryable_off_propagates_transient_error():
    """Default behavior (retryable=False) — exceptions go through as-is so
    arq applies its own max_tries logic without a deferral."""
    with patch(
        "aggrigator.ops.recorder.run_with_recording",
        side_effect=asyncio.TimeoutError("upstream stalled"),
    ):
        @cron_run_recorder("ingest_due_leagues")  # retryable defaults to False
        async def task(ctx_):
            ...

        with pytest.raises(asyncio.TimeoutError):
            await task(_ctx())


@pytest.mark.asyncio
async def test_retryable_on_converts_transient_to_retry():
    """retryable=True + retries available → arq.Retry with the configured defer."""
    with patch(
        "aggrigator.ops.recorder.run_with_recording",
        side_effect=RateLimitExceededError("bucket exhausted"),
    ):
        @cron_run_recorder("ingest_due_leagues", retryable=True, retry_defer_seconds=42)
        async def task(ctx_):
            ...

        with pytest.raises(Retry) as excinfo:
            await task(_ctx(job_try=1, max_tries=3))
        assert excinfo.value.defer_score == 42 * 1000  # arq stores defer in ms


@pytest.mark.asyncio
async def test_retryable_on_final_attempt_propagates():
    """retryable=True but we're already on the last try → no more retries,
    re-raise the original so arq marks the job permanently failed."""
    with patch(
        "aggrigator.ops.recorder.run_with_recording",
        side_effect=RateLimitExceededError("bucket exhausted"),
    ):
        @cron_run_recorder("ingest_due_leagues", retryable=True)
        async def task(ctx_):
            ...

        with pytest.raises(RateLimitExceededError):
            await task(_ctx(job_try=2, max_tries=2))


@pytest.mark.asyncio
async def test_terminal_error_never_retries():
    """Non-retryable exceptions (e.g. ValidationError = upstream HTTP 400)
    propagate unchanged even when retryable=True. Retrying a 400 just
    burns API quota — the request shape is wrong, more attempts won't fix it."""
    with patch(
        "aggrigator.ops.recorder.run_with_recording",
        side_effect=ValidationError("oddsapi 400: bad date format"),
    ):
        @cron_run_recorder("ingest_due_leagues", retryable=True)
        async def task(ctx_):
            ...

        with pytest.raises(ValidationError):
            await task(_ctx(job_try=1, max_tries=3))


@pytest.mark.asyncio
async def test_no_arq_context_propagates():
    """When called outside an arq worker (manual shell, /ops/crons trigger)
    there's no job_try/max_tries in ctx. Don't synthesize retry semantics —
    let the caller see the real exception."""
    with patch(
        "aggrigator.ops.recorder.run_with_recording",
        side_effect=asyncio.TimeoutError(),
    ):
        @cron_run_recorder("ingest_due_leagues", retryable=True)
        async def task(ctx_):
            ...

        with pytest.raises(asyncio.TimeoutError):
            await task({"job_id": "manual"})  # no job_try/max_tries
