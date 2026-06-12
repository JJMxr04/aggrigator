"""Prometheus ``/metrics`` endpoint.

Lightweight: register the standard process collector + a small set of custom
counters. Gated by ``AGG_METRICS_TOKEN`` (plan §6.2 P0-4): when set, the
scraper must send it as a bearer token (or ``?token=``), and a mismatch
returns **404** — the hide-existence pattern the profiler endpoints already
use, so probes can't even confirm the path exists. When unset, the endpoint
is open and a prod boot warning says so (the Coolify network isn't
guaranteed to firewall it).
"""

from __future__ import annotations

import hmac
import logging

from fastapi import FastAPI, HTTPException, Request, Response, status

logger = logging.getLogger(__name__)


def _token_ok(request: Request, expected: str) -> bool:
    supplied = ""
    auth = request.headers.get("authorization") or ""
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and value:
        supplied = value
    if not supplied:
        supplied = request.query_params.get("token") or ""
    if not supplied:
        return False
    return hmac.compare_digest(supplied, expected)


def register_metrics(app: FastAPI) -> None:
    """Mount /metrics. No-op if prometheus_client isn't installed."""
    try:
        from prometheus_client import (
            CONTENT_TYPE_LATEST,
            CollectorRegistry,
            generate_latest,
            multiprocess,
        )
    except ImportError:
        logger.info("prometheus_client not installed; /metrics disabled")
        return

    from aggrigator.config import get_settings

    settings = get_settings()
    if not settings.metrics_token and settings.env in ("prod", "production"):
        logger.warning(
            "[startup] AGG_METRICS_TOKEN unset in prod — /metrics is "
            "world-readable. Set a token and configure the Prometheus "
            "scraper to send it as a bearer token."
        )

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request) -> Response:
        token = get_settings().metrics_token
        if token and not _token_ok(request, token):
            # 404, not 401 — don't confirm the endpoint exists.
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
        registry = CollectorRegistry()
        try:
            multiprocess.MultiProcessCollector(registry)
        except Exception:  # noqa: BLE001
            # Single-process mode — use the default registry.
            from prometheus_client import REGISTRY
            registry = REGISTRY
        return Response(
            content=generate_latest(registry),
            media_type=CONTENT_TYPE_LATEST,
        )
