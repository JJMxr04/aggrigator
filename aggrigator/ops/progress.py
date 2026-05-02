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
_CANCEL_KEY_PREFIX = "aggrigator:croncancel:"
_TTL_SECONDS = 3600  # 1h cap; cron-run rows persist independently in Postgres


def _key(run_id: uuid.UUID | str) -> str:
    return f"{_KEY_PREFIX}{run_id}"


def _cancel_key(run_id: uuid.UUID | int | str) -> str:
    return f"{_CANCEL_KEY_PREFIX}{run_id}"


class CronCancelled(Exception):
    """Raised by ``raise_if_cancelled`` when an operator has clicked Stop.

    The recorder catches this distinctly from generic exceptions so the
    cron_run row gets ``status=CANCELLED`` (not ``FAILED``) and no
    traceback is stored — a stop isn't an error.
    """


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


# ---- cancellation (cooperative) -------------------------------------------


async def request_cancel(run_id: uuid.UUID | int | str) -> bool:
    """Set the cancel flag for ``run_id``. Returns True on success.

    Cancellation is COOPERATIVE — the runner only stops at points where
    it calls ``raise_if_cancelled``. So the latency between Stop click
    and actual halt is bounded by the time between checks (typically
    one league iteration in ``ingest_due_leagues``).
    """
    settings = get_settings()
    r = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await r.setex(_cancel_key(run_id), _TTL_SECONDS, "1")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("request_cancel failed: %s", exc)
        return False
    finally:
        try:
            await r.aclose()
        except Exception:  # noqa: BLE001
            pass


async def is_cancelled(run_id: uuid.UUID | int | str) -> bool:
    """True if the cancel flag is set for ``run_id``. Read failures
    return False — we never *spuriously* cancel a healthy run on a
    Redis blip."""
    settings = get_settings()
    r = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        return bool(await r.exists(_cancel_key(run_id)))
    except Exception as exc:  # noqa: BLE001
        logger.debug("is_cancelled read failed (treating as not cancelled): %s", exc)
        return False
    finally:
        try:
            await r.aclose()
        except Exception:  # noqa: BLE001
            pass


async def clear_cancel(run_id: uuid.UUID | int | str) -> None:
    """Delete the cancel key. Best-effort — TTL handles cleanup."""
    settings = get_settings()
    r = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await r.delete(_cancel_key(run_id))
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            await r.aclose()
        except Exception:  # noqa: BLE001
            pass


async def raise_if_cancelled() -> None:
    """Cron-runner-facing breakpoint. Raises ``CronCancelled`` if the
    operator has clicked Stop. No-op outside a cron context.

    Sprinkle these at natural pause points (between leagues, between
    batches). Don't bother adding to tight inner loops — Redis round-trip
    per check would dominate the cron's runtime.
    """
    rid = current_run_id.get()
    if rid is None:
        return
    if await is_cancelled(rid):
        # Update progress so the UI sees "stopping..." even before the
        # exception unwinds the call stack and the recorder records it.
        await set_progress("cancellation requested — stopping at next safe point")
        raise CronCancelled(f"cron run {rid} cancelled by operator")
