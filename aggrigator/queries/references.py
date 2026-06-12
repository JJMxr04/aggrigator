"""Reference-table reads (plan §6.8 step 2) — sports / leagues /
bookmakers / market types."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.models import Bookmaker, League, Market, Sport


class ReferenceQueries:
    """Stateless — pass the session per call."""

    async def list_sports(
        self, session: AsyncSession, *, active: bool | None = None,
    ) -> list[Sport]:
        stmt = select(Sport).order_by(Sport.name)
        if active is not None:
            stmt = stmt.where(Sport.active == active)
        return list(await session.scalars(stmt))

    async def list_leagues(
        self,
        session: AsyncSession,
        *,
        sport_id: str | None = None,
        active: bool | None = None,
    ) -> list[League]:
        stmt = select(League).order_by(League.sport_id, League.name)
        if sport_id is not None:
            stmt = stmt.where(League.sport_id == sport_id)
        if active is not None:
            stmt = stmt.where(League.active == active)
        return list(await session.scalars(stmt))

    async def list_bookmakers(
        self, session: AsyncSession, *, active: bool | None = None,
    ) -> list[Bookmaker]:
        stmt = select(Bookmaker).order_by(Bookmaker.name)
        if active is not None:
            stmt = stmt.where(Bookmaker.active == active)
        return list(await session.scalars(stmt))

    async def list_market_types(
        self, session: AsyncSession, *, sport_id: str | None = None,
    ) -> list[str]:
        stmt = select(Market.type).distinct().order_by(Market.type)
        if sport_id is not None:
            stmt = stmt.where(Market.sport_id == sport_id)
        return [t for t in await session.scalars(stmt) if t]
