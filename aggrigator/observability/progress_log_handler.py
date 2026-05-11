"""Forward Python logging records into the cron progress stream.

The aggregator runs as two processes (FastAPI + ARQ worker). Logs from
the worker's ingest pipeline previously only appeared in the worker's
stdout — operators watching ``/ops/crons`` saw a spinner without
detail. This handler bridges the gap:

- Every ``logging.LogRecord`` emitted by an attached logger is also
  pushed to the cron progress stream via ``ops.progress.set_progress``.
- ``set_progress`` writes to Redis (list + pub/sub), which the FastAPI
  SSE endpoint streams to the browser.
- So the same log line shows up in BOTH the worker terminal AND the
  /ops/crons live log — without ever round-tripping through HTTP.

Outside a cron run (no ``current_run_id`` set), set_progress is a
no-op. So API-side and one-shot script logs aren't affected.

Failures inside the handler are swallowed — logging that breaks the
process logging is doing is a worse bug than missing log lines.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Iterable

from aggrigator.ops.progress import current_run_id, set_progress

# Loggers whose records get mirrored to the progress stream. Keep this
# tight — root-logger mirroring would flood the UI with httpx/asyncpg
# chatter the operator doesn't need. Add modules here when their logs
# would help debug a live cron.
DEFAULT_FORWARD_LOGGERS: tuple[str, ...] = (
    "aggrigator.ingest",
    "aggrigator.workers",
    "aggrigator.webhooks",
    "aggrigator.ops",
)


class ProgressForwardHandler(logging.Handler):
    """Logging handler that mirrors records into the cron progress stream.

    Only fires when a cron run is active (``current_run_id`` is set in
    the ContextVar). Best-effort: errors are silently swallowed so
    logging itself can never crash the worker.
    """

    def __init__(self, level: int = logging.INFO) -> None:
        super().__init__(level=level)

    def emit(self, record: logging.LogRecord) -> None:
        if current_run_id.get() is None:
            return
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001 — never let logging break
            return
        try:
            _schedule_set_progress(msg)
        except Exception:  # noqa: BLE001 — same
            pass


def _schedule_set_progress(msg: str) -> None:
    """Schedule ``set_progress(msg)`` on the running event loop.

    ``logging.Handler.emit`` is sync (called from any thread / coroutine
    state). ``set_progress`` is async. We schedule it on the active loop
    without blocking. If no loop is running (rare — e.g. logs emitted
    during interpreter shutdown), drop the record.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    # ensure_future on the running loop. Result is discarded.
    loop.create_task(set_progress(msg))


def install_progress_forwarding(
    *, loggers: Iterable[str] = DEFAULT_FORWARD_LOGGERS, level: int = logging.INFO,
) -> ProgressForwardHandler:
    """Attach a single ``ProgressForwardHandler`` to each named logger.

    Idempotent — re-running doesn't stack handlers. Call once from
    ``configure_logging`` at process startup.
    """
    handler = ProgressForwardHandler(level=level)
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    for name in loggers:
        lg = logging.getLogger(name)
        # Avoid double-install on hot reload.
        existing = [h for h in lg.handlers if isinstance(h, ProgressForwardHandler)]
        for h in existing:
            lg.removeHandler(h)
        lg.addHandler(handler)
        # Don't change propagation — root handlers still emit to stdout.
    return handler
