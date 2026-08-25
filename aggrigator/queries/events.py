"""Event + market reads — behavior-identical port of
the inline ORM previously in ``api/events.py``."""

from __future__ import annotations

from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from aggrigator.models import BookmakerSelection, Event, Market, Selection
from aggrigator.queries.filters import EventListFilters, MarketFilters

# Mirrors MDProject's `markets.py` SCOPE_SUBJECT_SHORTCUT — keep in sync.
SCOPE_SUBJECT_SHORTCUT = {
    "game": ["MONEYLINE", "SPREAD", "TOTAL", "PROPS_GAME"],
    "team": ["PROPS_TEAM"],
}


def _csv(raw: str | None) -> list[str]:
    return [v for v in (raw.split(",") if raw else []) if v]


def _bod(d: Date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


_MARKET_EAGER = (
    selectinload(Market.selections)
        .selectinload(Selection.by_book)
        .selectinload(BookmakerSelection.bookmaker),
    selectinload(Market.subject_team),
)


class EventQueries:
    """Stateless — pass the session per call."""

    @staticmethod
    def _list_stmt(f: EventListFilters):
        stmt = (
            select(Event)
            # Listing shows only events with odds coverage — an event
            # without markets is unusable to every consumer.
            .where(Event.markets.any())
            .options(
                selectinload(Event.home_team),
                selectinload(Event.away_team),
                selectinload(Event.sport),
                selectinload(Event.league),
            )
        )
        if f.sport:
            stmt = stmt.where(Event.sport_id == f.sport)
        if f.league:
            stmt = stmt.where(Event.league_id == f.league)
        if f.live is True:
            stmt = stmt.where(Event.status_type == "inprogress")
        elif f.live is False:
            stmt = stmt.where(Event.status_type != "inprogress")

        # Window resolution: `date` wins (single-day filter), else honor
        # explicit starts_after / starts_before, else the 3h-ago →
        # 3-days-out default.
        if f.date is not None:
            start = _bod(f.date)
            stmt = stmt.where(
                Event.start_time >= start,
                Event.start_time < start + timedelta(days=1),
            )
        elif f.starts_after is not None or f.starts_before is not None:
            if f.starts_after is not None:
                stmt = stmt.where(Event.start_time >= f.starts_after)
            if f.starts_before is not None:
                stmt = stmt.where(Event.start_time < f.starts_before)
        else:
            now = datetime.now(tz=timezone.utc)
            stmt = stmt.where(
                Event.start_time >= now - timedelta(hours=3),
                Event.start_time < now + timedelta(days=3),
            )
        return stmt.order_by(Event.start_time)

    async def list_window(
        self,
        session: AsyncSession,
        f: EventListFilters,
        *,
        offset: int,
        limit: int,
    ) -> tuple[int, list[Event]]:
        stmt = self._list_stmt(f)
        total = await session.scalar(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        ) or 0
        rows = list(await session.scalars(stmt.offset(offset).limit(limit)))
        return int(total), rows

    async def get_detail(self, session: AsyncSession, event_id: str) -> Event | None:
        return await session.scalar(
            select(Event)
            .where(Event.id == event_id)
            .options(
                selectinload(Event.home_team),
                selectinload(Event.away_team),
                selectinload(Event.sport),
                selectinload(Event.league),
            )
        )

    async def get(self, session: AsyncSession, event_id: str) -> Event | None:
        return await session.get(Event, event_id)

    async def markets_for_event(
        self, session: AsyncSession, event_id: str, f: MarketFilters,
    ) -> list[Market]:
        """Raises ``ValueError`` on non-numeric min/max_decimal — the
        route maps it to the legacy 400."""
        stmt = (
            select(Market)
            .where(Market.event_id == event_id)
            .options(*_MARKET_EAGER)
        )
        if f.category:
            stmt = stmt.where(Market.category.in_(_csv(f.category)))
        if f.scope_subject:
            stmt = stmt.where(Market.category.in_(SCOPE_SUBJECT_SHORTCUT[f.scope_subject]))
        if f.scope:
            stmt = stmt.where(Market.scope.in_(_csv(f.scope)))
        if f.type:
            stmt = stmt.where(Market.type == f.type)
        if f.live is not None:
            stmt = stmt.where(Market.is_live == f.live)
        if f.team_id is not None:
            stmt = stmt.where(Market.subject_team_id == f.team_id)

        if f.settled is not None or f.min_decimal is not None or f.max_decimal is not None:
            sel_stmt = select(Selection.market_id)
            if f.settled is True:
                sel_stmt = sel_stmt.where(
                    Selection.settlement_status.in_(["WON", "LOST", "PUSH", "VOID"])
                )
            elif f.settled is False:
                sel_stmt = sel_stmt.where(Selection.settlement_status == "PENDING")
            try:
                if f.min_decimal is not None:
                    sel_stmt = sel_stmt.where(
                        Selection.decimal_odds >= Decimal(f.min_decimal)
                    )
                if f.max_decimal is not None:
                    sel_stmt = sel_stmt.where(
                        Selection.decimal_odds <= Decimal(f.max_decimal)
                    )
            except InvalidOperation:
                raise ValueError("min_decimal/max_decimal must be numeric")
            stmt = stmt.where(Market.id.in_(sel_stmt))

        stmt = stmt.order_by(Market.category, Market.scope, Market.line)
        return list(await session.scalars(stmt))

    async def markets_for_events(
        self, session: AsyncSession, event_ids: list[str],
    ) -> list[Market]:
        """Bulk eager-loaded markets for ``include=markets`` listings."""
        if not event_ids:
            return []
        stmt = (
            select(Market)
            .where(Market.event_id.in_(event_ids))
            .options(*_MARKET_EAGER)
            .order_by(Market.event_id, Market.category, Market.scope, Market.line)
        )
        return list(await session.scalars(stmt))
