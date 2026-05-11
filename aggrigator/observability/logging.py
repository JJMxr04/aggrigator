"""structlog configuration + cron-progress forwarding."""

from __future__ import annotations

import logging
import sys

import structlog

from aggrigator.observability.progress_log_handler import (
    install_progress_forwarding,
)


def configure_logging(level: str = "INFO") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
        level=log_level,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )
    # Mirror ingest / worker logs into the cron progress stream so the
    # /ops/crons SSE log surfaces the same lines the worker prints.
    # No-op outside a cron run context — API-side request logs stay in
    # their own terminal as before.
    install_progress_forwarding(level=log_level)
