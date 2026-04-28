"""Sentry init.

Auto-redacts ``Authorization``, ``X-Api-Key``, ``X-Aggrigator-Signature``
header values from event payloads (plan §8).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def init_sentry(settings) -> None:
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
    except ImportError:
        logger.warning("sentry_sdk not installed; skipping init")
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.env,
        integrations=[FastApiIntegration()],
        traces_sample_rate=0.05,
        profiles_sample_rate=0.0,
        before_send=_scrub_pii,
    )


_REDACTED_HEADERS = {
    "authorization", "x-api-key", "x-aggrigator-signature", "cookie",
}


def _scrub_pii(event, hint):
    request = event.get("request") or {}
    headers = request.get("headers") or {}
    if isinstance(headers, dict):
        for key in list(headers):
            if key.lower() in _REDACTED_HEADERS:
                headers[key] = "[redacted]"
    return event
