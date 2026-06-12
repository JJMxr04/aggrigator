"""GlitchTip error tracking via sentry-sdk.

``init_sentry(settings)`` is called explicitly from BOTH entrypoints —
``main.create_app()`` (web) and ``workers/app.py`` at module level (the
worker never imports ``main``) — mirroring how logging is configured.
The web process imports ``workers.app`` for ``defer_async``, so the call
is guarded to make the inevitable double-invocation a no-op.

Inert by default: an empty ``AGG_SENTRY_DSN`` means no SDK init at all,
so local dev and tests carry zero sentry behavior.

Scrubbing: GlitchTip must never store provider API keys. ``before_send``
walks every string in the outgoing event (request URL, query string,
breadcrumb messages, log lines, exception values — wherever it hides)
and applies the same ``apiKey=`` redaction regex the logging filter uses
(``observability/logging.py``), so the two redaction paths can't drift.
"""

from __future__ import annotations

import logging
import os

from aggrigator.observability.logging import _APIKEY_RE

logger = logging.getLogger(__name__)

_initialized = False

# Belt-and-suspenders bound on the scrub recursion. Sentry events are
# shallow (< 10 levels); anything deeper is pathological and gets left
# as-is rather than risking a RecursionError inside before_send.
_MAX_SCRUB_DEPTH = 32


def _scrub(value, depth: int = 0):
    """Redact ``apiKey=<value>`` from every string in the event payload."""
    if depth > _MAX_SCRUB_DEPTH:
        return value
    if isinstance(value, str):
        return _APIKEY_RE.sub(r"\1REDACTED", value)
    if isinstance(value, dict):
        return {k: _scrub(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(v, depth + 1) for v in value)
    return value


def _before_send(event, hint):
    try:
        return _scrub(event)
    except Exception:  # noqa: BLE001 — a scrub bug must never drop the event
        logger.exception("[sentry] before_send scrub failed; sending unscrubbed event")
        return event


def init_sentry(settings) -> None:
    """Initialize sentry-sdk if a DSN is configured; otherwise do nothing.

    Safe to call more than once per process — only the first call with a
    DSN takes effect.
    """
    global _initialized
    if _initialized or not settings.sentry_dsn:
        return

    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.env,
        # Coolify passes SOURCE_COMMIT as a build arg; the Dockerfiles
        # bake it into the image env so events carry the deployed SHA.
        release=os.environ.get("SOURCE_COMMIT") or None,
        # Errors only — no PII, no perf tracing. FastAPI/Starlette and
        # logging integrations are enabled automatically by the SDK.
        send_default_pii=False,
        traces_sample_rate=0,
        before_send=_before_send,
    )
    _initialized = True
    logger.info("[sentry] initialized env=%s", settings.env)
