"""Async SQLAlchemy 2.0 engine + session factory.

Every request handler / worker job that touches the DB acquires a session via
``get_session``. Background jobs grab one with ``async with session_scope()``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from aggrigator.config import get_settings

_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    echo=False,
    # pool_pre_ping issues a lightweight SELECT 1 before each checkout,
    # transparently reconnecting if a Neon pooler timed out the
    # underlying connection. Without this the next request after an
    # idle period 500s instead of recovering.
    pool_pre_ping=True,
    # pool_size + max_overflow tune the asyncpg pool. Conservative
    # defaults assume ~3 workers × ~10 concurrent requests = ~30
    # connections. Neon free tier allows 100, paid plans much more —
    # bump if you scale workers up.
    pool_size=10,
    max_overflow=20,
    # pool_recycle below the typical PgBouncer/Neon idle-close (Neon
    # closes ~5 min) so we don't ever hand out a connection that's
    # about to expire upstream.
    pool_recycle=240,
)

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with async_session_factory() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Worker / script context manager — commits on clean exit, rolls back on error."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
