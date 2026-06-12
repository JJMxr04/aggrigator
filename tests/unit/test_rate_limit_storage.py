"""Storage resilience for security/rate_limit — the failure modes that
bit the first prod deploy (missing coredis) plus runtime outages."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aggrigator.security import rate_limit


def _settings(url: str, env: str = "prod") -> SimpleNamespace:
    return SimpleNamespace(
        ratelimit_enabled=True, ratelimit_storage_url=url, env=env,
    )


def test_redis_storage_constructs() -> None:
    """The async-redis extra (coredis) must be installed — this is the
    exact failure that crashed the 2026-06-12 deploy."""
    rate_limit.init_rate_limiter(_settings("redis://localhost:6379"))
    assert type(rate_limit._storage).__name__ == "RedisStorage"


def test_bogus_storage_url_falls_back_to_memory() -> None:
    rate_limit.init_rate_limiter(_settings("not-a-real-scheme://nope"))
    assert type(rate_limit._storage).__name__ == "MemoryStorage"
    assert rate_limit._limiter is not None  # still limiting, per-process


@pytest.mark.asyncio
async def test_backend_error_fails_open(monkeypatch) -> None:
    """Redis outage at request time → allow traffic, don't 500."""
    rate_limit.init_rate_limiter(_settings("memory://", env="dev"))

    async def boom(*a, **k):
        raise ConnectionError("redis is gone")

    monkeypatch.setattr(rate_limit._limiter, "hit", boom)
    retry = await rate_limit.check(rate_limit.AUTH_RULE, "t", "1.2.3.4")
    assert retry is None  # fail open


@pytest.fixture(autouse=True)
def _restore_limiter():
    yield
    # Re-init with defaults so later tests see the normal memory limiter.
    rate_limit.init_rate_limiter(
        SimpleNamespace(ratelimit_enabled=True, ratelimit_storage_url="", env="dev")
    )
