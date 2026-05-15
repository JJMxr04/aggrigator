"""Integration-test fixtures: real Postgres + ASGI client.

Skips the entire integration suite when ``AGG_TEST_DATABASE_URL`` isn't set or
the DB isn't reachable. Run ``docker compose up -d postgres`` (the project's
own ``docker-compose.yml``) and ``alembic upgrade head`` first; tests then run
in transactional fixtures so each test rolls back cleanly.

Each test gets:
- a fresh ``AsyncSession`` whose changes are rolled back at the end
- an ``httpx.AsyncClient`` against the FastAPI ASGI app, with the
  ``get_session`` dep overridden to use the test session
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aggrigator.db import get_session
from aggrigator.main import app


def _async_url() -> str | None:
    return os.environ.get("AGG_TEST_DATABASE_URL")


def _has_db() -> bool:
    return bool(_async_url())


pytestmark = pytest.mark.skipif(
    not _has_db(),
    reason="Set AGG_TEST_DATABASE_URL to run integration tests "
    "(e.g. postgresql+asyncpg://aggrigator:aggrigator@localhost:5433/aggrigator)",
)


@pytest_asyncio.fixture(scope="session")
async def engine():
    from sqlalchemy import text

    url = _async_url()
    if not url:
        pytest.skip("no AGG_TEST_DATABASE_URL")
    eng = create_async_engine(url, pool_pre_ping=True)
    # Lightweight reachability probe — fail fast with a clear skip if Postgres
    # isn't actually running.
    try:
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await eng.dispose()
        pytest.skip(f"AGG_TEST_DATABASE_URL unreachable: {exc}")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine):
    """One session per test, with everything rolled back at the end."""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        # Truncate every aggregator-owned table at the start of each test —
        # simpler than savepoint-based rollback when API handlers commit.
        await _wipe(session)
        yield session
        await session.rollback()


async def _wipe(session) -> None:
    # Order matters: children first.
    tables = [
        "audit_log",
        "webhook_delivery",
        "auth_refresh_token",
        "auth_user",
        "core_odds_quote",
        "core_bookmaker_selection",
        "core_selection",
        "core_market",
        "core_bookmaker",
        "core_event_event",
        "core_team",
        "core_league",
        "core_sport",
    ]
    from sqlalchemy import text
    for t in tables:
        await session.execute(text(f"TRUNCATE TABLE {t} CASCADE"))
    await session.commit()


@pytest_asyncio.fixture
async def client(session):
    """ASGI client that routes the request's session through our test session."""
    async def _override():
        yield session
    app.dependency_overrides[get_session] = _override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)
