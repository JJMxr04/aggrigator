"""Redis lock for cron-trigger debounce.

Per plan §2.1.6 — chose Redis over Postgres advisory locks because (1) Redis
is already in the stack as the ARQ broker, (2) ``SET NX EX`` self-expires on
worker crash, (3) PG advisory locks scope to the connection lifetime, which
is the wrong shape for "run lifetime" exclusivity.

Key: ``aggrigator:cronlock:{cron_name}`` — one lock per cron, not per trigger.
Value: ``run_id`` (so the unlock can verify ownership before deleting).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# Atomic compare-and-delete — only release a lock we still own.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


def _key(cron_name: str) -> str:
    return f"aggrigator:cronlock:{cron_name}"


async def try_acquire(
    redis: Redis, cron_name: str, run_id: str, *, ttl_seconds: int,
) -> bool:
    """``SET key run_id NX EX ttl`` — atomic. Returns True if we got the lock."""
    ok = await redis.set(_key(cron_name), run_id, nx=True, ex=ttl_seconds)
    return bool(ok)


async def release(redis: Redis, cron_name: str, run_id: str) -> bool:
    """Release only if we still own the lock (ie. our run_id matches)."""
    try:
        result = await redis.eval(_RELEASE_SCRIPT, 1, _key(cron_name), run_id)
        return bool(result)
    except Exception:  # noqa: BLE001
        logger.exception("lock release failed for cron=%s run=%s", cron_name, run_id)
        return False


async def is_locked(redis: Redis, cron_name: str) -> bool:
    return bool(await redis.exists(_key(cron_name)))


@asynccontextmanager
async def cron_lock(
    redis: Redis, cron_name: str, run_id: str, *, ttl_seconds: int,
) -> AsyncIterator[bool]:
    """Context manager — acquire on enter, release on exit.

    Yields ``True`` if acquired (caller proceeds), ``False`` if not (caller
    surfaces 409). Either way, exit unlocks (no-op if we never owned it).
    """
    acquired = await try_acquire(redis, cron_name, run_id, ttl_seconds=ttl_seconds)
    try:
        yield acquired
    finally:
        if acquired:
            await release(redis, cron_name, run_id)
