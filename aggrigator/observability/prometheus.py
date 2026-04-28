"""Prometheus ``/metrics`` endpoint.

Lightweight: register the standard process collector + a small set of custom
counters. Production deployments typically scrape this from Prometheus inside
the cluster network — there's no auth wrapper here, so the endpoint should
be firewall'd off externally (plan §3.6).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Response

logger = logging.getLogger(__name__)


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

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
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
