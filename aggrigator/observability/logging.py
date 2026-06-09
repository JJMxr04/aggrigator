"""structlog configuration."""

from __future__ import annotations

import logging
import re
import sys

import structlog


# Matches ``apiKey=<value>`` / ``api_key=<value>`` up to the next separator.
# Stops at & (query delim), whitespace (httpx logs the URL then a space +
# the status line), and quotes — so the surrounding log line survives intact.
_APIKEY_RE = re.compile(r"(?i)(api_?key=)[^&\s\"'<>]+")


class _RedactApiKeyFilter(logging.Filter):
    """Strip ``apiKey=<value>`` from any log record before it is emitted.

    The odds-api HTTP client redacts its OWN log lines, but httpx logs every
    request URL at INFO level on the ``httpx`` logger — that line leaks the
    raw key (``GET .../leagues?...&apiKey=SECRET``). This filter rewrites the
    fully-formatted message so the redaction holds no matter which logger or
    handler emits it. Attached both to the ``httpx`` logger (so it survives
    uvicorn/gunicorn re-wiring their handlers) and to the root handler.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — never let logging redaction crash logging
            return True
        redacted = _APIKEY_RE.sub(r"\1REDACTED", message)
        if redacted != message:
            # Collapse to the pre-rendered, redacted string so args can't
            # re-introduce the secret downstream.
            record.msg = redacted
            record.args = ()
        return True


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
    # Redact apiKey from leaky third-party request logs. httpx logs the full
    # URL (with apiKey=) at INFO on the ``httpx`` logger; attach to that logger
    # directly so it holds through handler re-wiring, and to the root handler(s)
    # so anything else that logs a key is caught too.
    _redact = _RedactApiKeyFilter()
    logging.getLogger("httpx").addFilter(_redact)
    for _handler in logging.getLogger().handlers:
        _handler.addFilter(_redact)
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
