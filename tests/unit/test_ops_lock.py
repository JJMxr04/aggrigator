"""Unit tests for ops.lock — Redis lock primitive.

Uses a fakeredis-style in-memory async stub so the test doesn't need a live
Redis. The point is to exercise the acquire/release/compare-and-delete logic,
not Redis itself.
"""

from __future__ import annotations

from typing import Any

import pytest

from aggrigator.ops import lock as lock_module


class _FakeRedis:
    """Minimal async stub — supports the SET NX EX + EVAL surface lock uses."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.scripts_run: list[tuple] = []

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def eval(self, script: str, num_keys: int, *args: Any) -> int:
        # Faithful reproduction of the cmp+del Lua: only delete if value matches.
        key, value = args[0], args[1]
        if self.store.get(key) == value:
            del self.store[key]
            return 1
        return 0

    async def exists(self, key: str) -> int:
        return 1 if key in self.store else 0


@pytest.mark.asyncio
async def test_acquire_then_release_round_trip() -> None:
    r = _FakeRedis()
    ok = await lock_module.try_acquire(r, "ingest", "run-1", ttl_seconds=60)
    assert ok is True
    assert await lock_module.is_locked(r, "ingest")

    released = await lock_module.release(r, "ingest", "run-1")
    assert released is True
    assert not await lock_module.is_locked(r, "ingest")


@pytest.mark.asyncio
async def test_second_acquire_blocks_until_release() -> None:
    r = _FakeRedis()
    assert await lock_module.try_acquire(r, "ingest", "run-1", ttl_seconds=60) is True
    # Second caller can't acquire while run-1 holds it.
    assert await lock_module.try_acquire(r, "ingest", "run-2", ttl_seconds=60) is False
    await lock_module.release(r, "ingest", "run-1")
    # Now run-2 can.
    assert await lock_module.try_acquire(r, "ingest", "run-2", ttl_seconds=60) is True


@pytest.mark.asyncio
async def test_release_with_wrong_run_id_does_nothing() -> None:
    """If a stale worker tries to release someone else's lock, it must no-op."""
    r = _FakeRedis()
    await lock_module.try_acquire(r, "ingest", "run-1", ttl_seconds=60)
    released = await lock_module.release(r, "ingest", "run-imposter")
    assert released is False
    assert await lock_module.is_locked(r, "ingest")  # still held by run-1


@pytest.mark.asyncio
async def test_context_manager_releases_on_exit() -> None:
    r = _FakeRedis()
    async with lock_module.cron_lock(r, "ingest", "run-1", ttl_seconds=60) as ok:
        assert ok is True
        assert await lock_module.is_locked(r, "ingest")
    assert not await lock_module.is_locked(r, "ingest")


@pytest.mark.asyncio
async def test_context_manager_releases_on_exception() -> None:
    r = _FakeRedis()
    with pytest.raises(RuntimeError):
        async with lock_module.cron_lock(r, "ingest", "run-1", ttl_seconds=60) as ok:
            assert ok is True
            raise RuntimeError("boom")
    # Lock auto-released even though the body raised.
    assert not await lock_module.is_locked(r, "ingest")


@pytest.mark.asyncio
async def test_context_manager_yields_false_when_already_held() -> None:
    r = _FakeRedis()
    await lock_module.try_acquire(r, "ingest", "run-other", ttl_seconds=60)
    async with lock_module.cron_lock(r, "ingest", "run-mine", ttl_seconds=60) as ok:
        assert ok is False
    # The other lock is still in place.
    assert await lock_module.is_locked(r, "ingest")
