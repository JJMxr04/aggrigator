"""Unit tests for the cron_run recorder + pause gate.

Retry classification used to live in this decorator (translating transient
failures into ``arq.worker.Retry``). With Procrastinate that responsibility
moved to the per-task ``@app.task(retry=RetryStrategy(...))`` declaration —
the recorder no longer needs to know about retries, so those tests went
away. What remains is the behavior the recorder itself owns: pause-gate
short-circuit and re-raise on real exceptions.

These tests stub out ``run_with_recording`` so we can drive exception paths
without spinning up Postgres / the queue.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from aggrigator.ingest.odds_api_errors import RateLimitExceededError, ValidationError
from aggrigator.ops.recorder import cron_run_recorder


@pytest.fixture(autouse=True)
def _allow_scheduled_run():
    """Skip the cron_schedule.enabled DB lookup — these tests stub the
    recorder upstream of the actual SQL paths and don't run migrations."""
    with patch(
        "aggrigator.ops.recorder._scheduled_run_enabled",
        new=AsyncMock(return_value=True),
    ):
        yield


@pytest.fixture(autouse=True)
def _skip_advisory_lock():
    """The recorder takes a Postgres advisory lock to mutex with manual
    triggers; these unit tests don't have a DB, so make ``session_scope``
    yield a session whose ``scalar`` returns True (lock acquired) and
    whose ``execute`` is a no-op."""
    class _FakeSession:
        async def scalar(self, *_a, **_kw):
            return True

        async def execute(self, *_a, **_kw):
            return None

    class _FakeScope:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, *_a):
            return None

    with patch(
        "aggrigator.ops.recorder.session_scope",
        new=lambda: _FakeScope(),
    ):
        yield


@pytest.mark.asyncio
async def test_pause_gate_short_circuits_when_disabled():
    """When ``_scheduled_run_enabled`` returns False, the task body never
    runs — the wrapper returns a ``skipped`` marker dict and writes no
    cron_run row. Manual triggers bypass this wrapper entirely."""
    with patch(
        "aggrigator.ops.recorder._scheduled_run_enabled",
        new=AsyncMock(return_value=False),
    ), patch(
        "aggrigator.ops.recorder.run_with_recording",
        new=AsyncMock(),
    ) as mock_run:
        @cron_run_recorder("ingest_due_leagues")
        async def task(context=None):
            ...

        result = await task(context=None)
        assert result == {"skipped": True, "reason": "disabled"}
        mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_transient_error_propagates():
    """Retry policy is now declared on the task itself via
    ``@app.task(retry=RetryStrategy(...))``. The recorder just re-raises
    so Procrastinate's strategy fires — it no longer classifies."""
    with patch(
        "aggrigator.ops.recorder.run_with_recording",
        side_effect=asyncio.TimeoutError("upstream stalled"),
    ):
        @cron_run_recorder("ingest_due_leagues")
        async def task(context=None):
            ...

        with pytest.raises(asyncio.TimeoutError):
            await task(context=None)


@pytest.mark.asyncio
async def test_rate_limit_error_propagates():
    with patch(
        "aggrigator.ops.recorder.run_with_recording",
        side_effect=RateLimitExceededError("bucket exhausted"),
    ):
        @cron_run_recorder("ingest_due_leagues")
        async def task(context=None):
            ...

        with pytest.raises(RateLimitExceededError):
            await task(context=None)


@pytest.mark.asyncio
async def test_terminal_error_propagates():
    """Validation errors (upstream HTTP 400) are not retryable. They'd
    burn quota — propagate so Procrastinate marks the job failed."""
    with patch(
        "aggrigator.ops.recorder.run_with_recording",
        side_effect=ValidationError("oddsapi 400: bad date format"),
    ):
        @cron_run_recorder("ingest_due_leagues")
        async def task(context=None):
            ...

        with pytest.raises(ValidationError):
            await task(context=None)
