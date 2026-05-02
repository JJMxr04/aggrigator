"""Live progress messages for in-flight cron runs.

Why this exists: the ops console (``/ops/crons``) only shows a "running"
pill while a cron is in flight. For a 60-second `ingest_due_leagues`
walk that's a long time staring at a spinner with no signal that
anything is happening. This module gives runners a one-liner
``await set_progress("walking NFL events")`` and the UI surfaces an
append-only log of those messages — pushed in real time over SSE so
operators see updates as they happen, not on a poll tick.

Storage: Redis list (for backlog when an SSE client connects mid-run)
+ Redis pub/sub channel (for live push).

- Key   ``aggrigator:cronprogress:<run_id>``  — JSON entries, RPUSHed
  oldest-first, trimmed to the most-recent ``_MAX_MESSAGES``, 1h TTL.
- Chan  ``aggrigator:cronprogress:<run_id>:ch`` — same JSON payload
  PUBLISHed on every set_progress so SSE subscribers wake up
  immediately. The recorder publishes a ``__complete__`` sentinel when
  the run finishes so subscribers exit their loop cleanly.

Discovery: a ``ContextVar`` carries the current run_id ambiently. The
recorder sets it around ``spec.runner()`` so individual task functions
don't have to thread the id through their signature.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
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
_MAX_MESSAGES = 200  # keep the most-recent N updates per run (LTRIM bound)
COMPLETE_SENTINEL = "__complete__"


@dataclass(frozen=True)
class ProgressUpdate:
    message: str
    ts: str  # ISO8601 UTC, second precision


def _key(run_id: uuid.UUID | int | str) -> str:
    return f"{_KEY_PREFIX}{run_id}"


def _channel(run_id: uuid.UUID | int | str) -> str:
    return f"{_KEY_PREFIX}{run_id}:ch"


def _cancel_key(run_id: uuid.UUID | int | str) -> str:
    return f"{_CANCEL_KEY_PREFIX}{run_id}"


class CronCancelled(Exception):
    """Raised by ``raise_if_cancelled`` when an operator has clicked Stop.

    The recorder catches this distinctly from generic exceptions so the
    cron_run row gets ``status=CANCELLED`` (not ``FAILED``) and no
    traceback is stored — a stop isn't an error.
    """


async def set_progress(message: str) -> None:
    """Append a one-line progress update for the current cron run.

    Writes to two places:

    1. The Redis list at ``_key(run_id)`` — durable backlog so a client
       opening the page mid-run can replay everything that's happened.
    2. The Redis channel at ``_channel(run_id)`` — fan-out to live SSE
       subscribers so the UI updates without waiting for a poll.

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
        # Pipeline the list write so all three commands cost one round-trip.
        # Pub/sub is a separate command (not a multi-state op) so we issue
        # it after — order matters: subscribers should never see a message
        # that isn't yet in the backlog (otherwise replay-on-connect could
        # show duplicates or, worse, miss a message in the gap).
        async with r.pipeline(transaction=False) as pipe:
            pipe.rpush(_key(rid), payload)
            pipe.ltrim(_key(rid), -_MAX_MESSAGES, -1)
            pipe.expire(_key(rid), _TTL_SECONDS)
            await pipe.execute()
        await r.publish(_channel(rid), payload)
    except Exception as exc:  # noqa: BLE001 — never break runners on Redis blips
        logger.debug("set_progress failed (non-fatal): %s", exc)
    finally:
        try:
            await r.aclose()
        except Exception:  # noqa: BLE001
            pass


async def publish_complete(run_id: uuid.UUID | int | str) -> None:
    """Tell live SSE subscribers the run is over so they can close.

    Called from the recorder once ``spec.runner()`` returns (or raises).
    The sentinel is pub/sub-only — no list entry — because clients
    that connect *after* completion get the empty backlog and a
    closed channel (the SSE endpoint handles that case explicitly).
    """
    settings = get_settings()
    r = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await r.publish(_channel(run_id), COMPLETE_SENTINEL)
    except Exception as exc:  # noqa: BLE001
        logger.debug("publish_complete failed (non-fatal): %s", exc)
    finally:
        try:
            await r.aclose()
        except Exception:  # noqa: BLE001
            pass


async def get_progress(run_id: uuid.UUID | int | str) -> list[ProgressUpdate]:
    """Read the full message backlog for a run, oldest-first.

    The ops/crons UI calls this for rows whose status is RUNNING (to
    seed the initial render before SSE takes over). Read failures fall
    back to ``[]`` so a Redis blip never breaks the page.
    """
    settings = get_settings()
    r = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        raws = await r.lrange(_key(run_id), 0, -1)
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_progress read failed (non-fatal): %s", exc)
        return []
    finally:
        try:
            await r.aclose()
        except Exception:  # noqa: BLE001
            pass
    out: list[ProgressUpdate] = []
    for raw in raws or []:
        try:
            d = json.loads(raw)
            out.append(ProgressUpdate(message=d["message"], ts=d["ts"]))
        except (ValueError, TypeError, KeyError):
            continue
    return out


async def subscribe_progress(
    run_id: uuid.UUID | int | str,
) -> AsyncIterator[ProgressUpdate | None]:
    """Yield each new progress update as it's published.

    Yields ``ProgressUpdate`` for normal messages and a single ``None``
    when the recorder has signaled completion (via ``publish_complete``)
    — at which point the generator returns.

    The caller (the SSE endpoint) is responsible for handling client
    disconnect — wrap iteration in a try/finally and break on
    ``request.is_disconnected()``.
    """
    settings = get_settings()
    r = Redis.from_url(settings.redis_url, decode_responses=True)
    pubsub = r.pubsub()
    try:
        await pubsub.subscribe(_channel(run_id))
        async for msg in pubsub.listen():
            if msg.get("type") != "message":
                continue
            data = msg.get("data")
            if data == COMPLETE_SENTINEL:
                yield None
                return
            try:
                d = json.loads(data)
                yield ProgressUpdate(message=d["message"], ts=d["ts"])
            except (ValueError, TypeError, KeyError):
                continue
    finally:
        try:
            await pubsub.unsubscribe(_channel(run_id))
        except Exception:  # noqa: BLE001
            pass
        try:
            await pubsub.aclose()
        except Exception:  # noqa: BLE001
            pass
        try:
            await r.aclose()
        except Exception:  # noqa: BLE001
            pass


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
