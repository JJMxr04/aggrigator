"""Read the arq worker's heartbeat from Redis to answer "is the
background worker alive?" without having to tail Railway logs.

arq's worker writes a small string to ``arq:queue:health-check`` every
``health_check_interval`` seconds (we override that to 30s in
``aggrigator.workers.settings``). The key is set with ``psetex`` and a
TTL of ``(interval + 1) * 1000`` ms — so the key vanishes within ~31s
of the worker dying. That's our offline signal.

The value looks like::

    Mar-04 10:30:12 j_complete=12 j_failed=0 j_retried=0 j_ongoing=0 queued=0

We parse it loosely so an arq version bump that adds new counters won't
break the parser — unknown ``key=value`` pairs are kept as-is in the
``counters`` dict.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# arq's default queue_name + health_check_key_suffix. We don't override
# either in WorkerSettings, so this matches what the worker writes.
HEALTH_KEY = "arq:queue:health-check"

# How long since the last heartbeat before we call the worker stale.
# Worker writes every 30s (see WorkerSettings.health_check_interval),
# so 90s = three missed beats.
STALE_AFTER_SECONDS = 90

_COUNTER_RE = re.compile(r"(\w+)=(\S+)")


class WorkerState(str, Enum):
    ONLINE = "online"      # heartbeat present and fresh (<= STALE_AFTER_SECONDS)
    STALE = "stale"        # heartbeat present but old — worker hung or paused
    OFFLINE = "offline"    # no heartbeat key in Redis (worker not running / crashed)
    UNKNOWN = "unknown"    # Redis itself unreachable — can't tell


@dataclass
class WorkerStatus:
    state: WorkerState
    raw: str | None = None              # the raw heartbeat string, if any
    heartbeat_at: datetime | None = None  # parsed timestamp from the heartbeat
    age_seconds: float | None = None    # how long ago we read it
    counters: dict[str, str] = field(default_factory=dict)
    error: str | None = None            # populated when state=UNKNOWN

    @property
    def is_healthy(self) -> bool:
        return self.state is WorkerState.ONLINE

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "is_healthy": self.is_healthy,
            "heartbeat_at": (
                self.heartbeat_at.isoformat() if self.heartbeat_at else None
            ),
            "age_seconds": (
                round(self.age_seconds, 1) if self.age_seconds is not None else None
            ),
            "counters": self.counters,
            "raw": self.raw,
            "error": self.error,
        }


async def get_worker_status(redis: Redis) -> WorkerStatus:
    """Read + parse the arq health-check key. Never raises — Redis
    failures are caught and surfaced as ``WorkerState.UNKNOWN``.
    """
    try:
        raw = await redis.get(HEALTH_KEY)
    except Exception as exc:  # noqa: BLE001 — must not crash the page render
        logger.warning("worker_status: redis GET failed: %s: %s",
                       type(exc).__name__, exc)
        return WorkerStatus(
            state=WorkerState.UNKNOWN,
            error=f"{type(exc).__name__}: {exc}",
        )

    if raw is None:
        return WorkerStatus(state=WorkerState.OFFLINE)

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")

    counters = dict(_COUNTER_RE.findall(raw))
    heartbeat_at = _parse_arq_timestamp(raw)
    age = None
    state = WorkerState.ONLINE
    if heartbeat_at is not None:
        age = (datetime.now(tz=timezone.utc) - heartbeat_at).total_seconds()
        if age > STALE_AFTER_SECONDS:
            state = WorkerState.STALE

    return WorkerStatus(
        state=state,
        raw=raw,
        heartbeat_at=heartbeat_at,
        age_seconds=age,
        counters=counters,
    )


def _parse_arq_timestamp(raw: str) -> datetime | None:
    """arq emits ``Mon-DD HH:MM:SS`` (no year, no timezone). We assume
    the worker's clock is UTC (the container has no TZ set, so Python's
    ``datetime.now()`` reports UTC). Treats the year as the current
    year, with a one-day-back tolerance for the Jan-1 wrap edge case
    where the worker last beat at Dec-31 23:59 right before midnight.
    """
    head = raw.split(" j_", 1)[0].strip()
    now = datetime.now(tz=timezone.utc)
    for year in (now.year, now.year - 1):
        try:
            ts = datetime.strptime(f"{year} {head}", "%Y %b-%d %H:%M:%S")
            ts = ts.replace(tzinfo=timezone.utc)
            # Reject "future" timestamps from picking the wrong year on
            # the Jan-1 wrap — only the older candidate would land in
            # the future when the worker's beat was actually last year.
            if ts <= now:
                return ts
        except ValueError:
            continue
    return None
