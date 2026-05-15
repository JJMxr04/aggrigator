"""Event reads — list, detail, markets-for-event.

Same query contract as MDProject ``core/event/views/api/events.py`` and
``markets.py`` (plan §3.4). The aggregator does **not** trigger inline provider
calls on read (unlike MDProject's ``get_event_odds``); a future on-demand
refresh job is enqueued instead — wired in Phase 3.
"""

from __future__ import annotations

from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from aggrigator.config import get_settings
from aggrigator.deps import SessionDep
from aggrigator.ingest.lifecycle import compute_stale
from aggrigator.models import BookmakerSelection, Event, Market, Selection
from aggrigator.schemas.event import (
    EventDetailOut,
    EventOut,
    EventWithMarketsOut,
    OddsMeta,
)
from aggrigator.schemas.market import MarketOut
from aggrigator.schemas.pagination import Page, PageParams

router = APIRouter(prefix="/v1/events", tags=["events"])


# Mirrors MDProject's `markets.py` SCOPE_SUBJECT_SHORTCUT — keep in sync.
SCOPE_SUBJECT_SHORTCUT = {
    "game": ["MONEYLINE", "SPREAD", "TOTAL", "PROPS_GAME"],
    "team": ["PROPS_TEAM"],
}


def _csv(raw: str | None) -> list[str]:
    return [v for v in (raw.split(",") if raw else []) if v]


def _bod(d: Date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


@router.get("", response_model=Page[EventWithMarketsOut])
async def list_events(
    session: SessionDep,
    page_params: Annotated[PageParams, Depends()],
    sport: str | None = Query(default=None, description="sportID, e.g. FOOTBALL"),
    league: str | None = Query(default=None, description="leagueID, e.g. NFL"),
    live: bool | None = Query(default=None),
    date: Date | None = Query(default=None, description="YYYY-MM-DD"),
    include: Literal["markets"] | None = Query(default=None),
) -> Page:
    stmt = select(Event).options(
        selectinload(Event.home_team),
        selectinload(Event.away_team),
        selectinload(Event.sport),
        selectinload(Event.league),
    )

    if sport:
        stmt = stmt.where(Event.sport_id == sport)
    if league:
        stmt = stmt.where(Event.league_id == league)
    if live is True:
        stmt = stmt.where(Event.status_type == "inprogress")
    elif live is False:
        stmt = stmt.where(Event.status_type != "inprogress")
    if date is not None:
        start = _bod(date)
        end = start + timedelta(days=1)
        stmt = stmt.where(Event.start_time >= start, Event.start_time < end)
    else:
        # Default window matches MDProject: from 3h ago to 3 days out.
        now = datetime.now(tz=timezone.utc)
        stmt = stmt.where(
            Event.start_time >= now - timedelta(hours=3),
            Event.start_time < now + timedelta(days=3),
        )

    stmt = stmt.order_by(Event.start_time)

    total = await session.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ) or 0

    rows = list(await session.scalars(
        stmt.offset(page_params.offset).limit(page_params.page_size)
    ))

    if include == "markets":
        items: list[EventWithMarketsOut] = await _attach_markets(session, rows)
    else:
        # Don't validate-from-attribute — Event.markets is lazy="raise" and
        # would blow up. Build via the markets-less view, then wrap.
        items = [
            EventWithMarketsOut(**EventOut.model_validate(r).model_dump(), markets=[])
            for r in rows
        ]

    return Page.build(
        items=items,
        page=page_params.page,
        page_size=page_params.page_size,
        total=total,
    )


@router.get("/{event_id}", response_model=EventDetailOut)
async def get_event(
    session: SessionDep,
    event_id: str,
    include: Literal["markets"] | None = Query(default=None),
) -> EventDetailOut:
    event = await session.scalar(
        select(Event)
        .where(Event.id == event_id)
        .options(
            selectinload(Event.home_team),
            selectinload(Event.away_team),
            selectinload(Event.sport),
            selectinload(Event.league),
        )
    )
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")

    markets: list[MarketOut] = []
    if include == "markets":
        enriched = await _attach_markets(session, [event])
        markets = enriched[0].markets

    base = EventOut.model_validate(event)
    return EventDetailOut(
        **base.model_dump(),
        markets=markets,
        odds_meta=OddsMeta(
            stale=compute_stale(
                status_type=event.status_type,
                start_time=event.start_time,
                grace_hours=get_settings().lifecycle_stale_grace_hours,
            ),
            last_provider_refresh_at=event.last_provider_refresh_at,
        ),
    )


@router.get("/{event_id}/markets")
async def get_event_markets(
    session: SessionDep,
    event_id: str,
    category: str | None = Query(default=None, description="comma-separated"),
    scope: str | None = Query(default=None, description="comma-separated"),
    scope_subject: Literal["game", "team"] | None = Query(default=None),
    type: str | None = Query(default=None),
    live: bool | None = Query(default=None),
    team_id: str | None = Query(default=None),
    settled: bool | None = Query(default=None),
    min_decimal: str | None = Query(default=None),
    max_decimal: str | None = Query(default=None),
) -> dict:
    event = await session.get(Event, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")

    stmt = (
        select(Market)
        .where(Market.event_id == event_id)
        .options(
            selectinload(Market.selections)
                .selectinload(Selection.by_book)
                .selectinload(BookmakerSelection.bookmaker),
            selectinload(Market.subject_team),
        )
    )

    if category:
        stmt = stmt.where(Market.category.in_(_csv(category)))
    if scope_subject:
        stmt = stmt.where(Market.category.in_(SCOPE_SUBJECT_SHORTCUT[scope_subject]))
    if scope:
        stmt = stmt.where(Market.scope.in_(_csv(scope)))
    if type:
        stmt = stmt.where(Market.type == type)
    if live is not None:
        stmt = stmt.where(Market.is_live == live)
    if team_id is not None:
        stmt = stmt.where(Market.subject_team_id == team_id)

    if settled is not None or min_decimal is not None or max_decimal is not None:
        sel_stmt = select(Selection.market_id)
        if settled is True:
            sel_stmt = sel_stmt.where(
                Selection.settlement_status.in_(["WON", "LOST", "PUSH", "VOID"])
            )
        elif settled is False:
            sel_stmt = sel_stmt.where(Selection.settlement_status == "PENDING")
        try:
            if min_decimal is not None:
                sel_stmt = sel_stmt.where(Selection.decimal_odds >= Decimal(min_decimal))
            if max_decimal is not None:
                sel_stmt = sel_stmt.where(Selection.decimal_odds <= Decimal(max_decimal))
        except InvalidOperation:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "min_decimal/max_decimal must be numeric",
            )
        stmt = stmt.where(Market.id.in_(sel_stmt))

    stmt = stmt.order_by(Market.category, Market.scope, Market.line)
    rows = list(await session.scalars(stmt))
    return {
        "markets": [MarketOut.model_validate(m) for m in rows],
        "odds_meta": {
            "stale": compute_stale(
                status_type=event.status_type,
                start_time=event.start_time,
                grace_hours=get_settings().lifecycle_stale_grace_hours,
            ),
            "last_provider_refresh_at": event.last_provider_refresh_at,
        },
    }


# ---- helpers ---------------------------------------------------------------


async def _attach_markets(session, events: list[Event]) -> list[EventWithMarketsOut]:
    """Bulk-load markets+selections for a list of events, return enriched dtos."""
    if not events:
        return []
    event_ids = [e.id for e in events]
    stmt = (
        select(Market)
        .where(Market.event_id.in_(event_ids))
        .options(
            selectinload(Market.selections)
                .selectinload(Selection.by_book)
                .selectinload(BookmakerSelection.bookmaker),
            selectinload(Market.subject_team),
        )
        .order_by(Market.event_id, Market.category, Market.scope, Market.line)
    )
    markets = list(await session.scalars(stmt))
    by_event: dict[str, list[Market]] = {}
    for m in markets:
        by_event.setdefault(m.event_id, []).append(m)

    out: list[EventWithMarketsOut] = []
    for e in events:
        body = EventOut.model_validate(e).model_dump()
        out.append(EventWithMarketsOut(
            **body,
            markets=[MarketOut.model_validate(m) for m in by_event.get(e.id, [])],
        ))
    return out
