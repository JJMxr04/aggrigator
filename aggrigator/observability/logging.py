"""structlog configuration."""

from __future__ import annotations

import logging
import sys

import structlog


class _SuppressHealthcheckAccessFilter(logging.Filter):
    """Drop ``uvicorn.access`` records for container health probes.

    Coolify polls ``/healthz`` (and ``/readyz``) every ~10s; without this
    the access log is mostly probe noise that buries real request lines.
    uvicorn logs access as ``'%s - "%s %s HTTP/%s" %d'`` with
    args ``(client_addr, method, full_path, http_version, status_code)``,
    so we inspect the path positionally.
    """

    _EXCLUDED = ("/healthz", "/readyz")

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, (tuple, list)) and len(args) >= 3:
            path = args[2]
            if isinstance(path, str) and path.split("?", 1)[0].rstrip("/") in self._EXCLUDED:
                return False
        return True


def configure_logging(level: str = "INFO") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
        level=log_level,
    )
    # Filter health-probe spam out of the access log. Attached to the
    # logger itself (not a handler) so it survives uvicorn/gunicorn wiring
    # up their own handlers later in worker boot.
    logging.getLogger("uvicorn.access").addFilter(_SuppressHealthcheckAccessFilter())
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
