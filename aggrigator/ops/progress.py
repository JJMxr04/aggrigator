"""Live progress messages for in-flight cron runs.

Why this exists: the ops console (``/ops/crons``) only shows a "running"
pill while a cron is in flight. For a 60-second `ingest_due_leagues`
walk that's a long time staring at a spinner with no signal that
anything is happening. This module gives runners a one-liner
``await set_progress("walking NFL events")`` that surfaces in the UI on
the next 2-second HTMX poll.

Storage: Redis. Key ``aggrigator:cronprogress:<run_id>``, value JSON
``{"message": str, "ts": iso}``. TTL caps at 1h so abandoned runs
self-clean.

Discovery: a ``ContextVar`` carries the current run_id ambiently. The
recorder sets it around ``spec.runner()`` so individual task functions
don't have to thread the id through their signature.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from redis.asyncio import Redis

from aggrigator.config import get_settings

logger = logging.getLogger(__name__)


# Set by ops/recorder.run_with_recording before invoking the runner; reset
# on exit. None means "not currently inside a cron run" (manual scripts,
# tests, ad-hoc shell calls) → set_progress is a no-op.
current_run_id: ContextVar[uuid.UUID | None] = ContextVar(
    "aggrigator_current_run_id", default=None,
)

_KEY_PREFIX = "aggrigator:cronprogress:"
_TTL_SECONDS = 3600  # 1h cap; cron-run rows persist independently in Postgres


def _key(run_id: uuid.UUID | str) -> str:
    return f"{_KEY_PREFIX}{run_id}"


async def set_progress(message: str) -> None:
    """Write a one-line progress update for the current cron run.

    No-op when called outside a cron run (ContextVar unset). Failures
    are swallowed — UI nicety should never break the cron itself.
    """
    rid = current_run_id.get()
    if rid is None:
        return
    settings = get_settings()
    payload = json.dumps({
        "message": message[:500],  # bound the size
        "ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    })
    r = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await r.setex(_key(rid), _TTL_SECONDS, payload)
    except Exception as exc:  # noqa: BLE001 — never break runners on Redis blips
        logger.debug("set_progress failed (non-fatal): %s", exc)
    finally:
        try:
            await r.aclose()
        except Exception:  # noqa: BLE001
            pass


async def get_progress(run_id: uuid.UUID | int | str) -> str | None:
    """Read the current progress message, or None if absent / Redis down.

    The ops/crons UI calls this for rows whose status is RUNNING. Read
    failures fall back to None — the UI just won't show a progress line.
    """
    settings = get_settings()
    r = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        raw = await r.get(_key(run_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_progress read failed (non-fatal): %s", exc)
        return None
    finally:
        try:
            await r.aclose()
        except Exception:  # noqa: BLE001
            pass
    if not raw:
        return None
    try:
        return json.loads(raw).get("message")
    except (ValueError, TypeError):
        return None


async def clear_progress(run_id: uuid.UUID | int | str) -> None:
    """Delete a run's progress key. Best-effort — Redis TTL handles cleanup
    even if this fails."""
    settings = get_settings()
    r = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await r.delete(_key(run_id))
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            await r.aclose()
        except Exception:  # noqa: BLE001
            pass
