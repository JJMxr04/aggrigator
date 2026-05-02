"""Persist normalized ``MarketSpec`` lists to the DB.

Async port of MDProject's ``core/event/odds/sgo_ingest.write_markets``.

Pure-spec parsing lives in ``normalize.py`` (Django-free, unit-testable).
This module is the side-effecting layer: every upsert is keyed on the
deterministic IDs ``normalize`` produces, so the writes are idempotent and a
re-ingest of the same payload doesn't create duplicate rows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.ingest.normalize import BookmakerQuoteSpec, MarketSpec, SelectionSpec
from aggrigator.models import (
    Bookmaker,
    BookmakerSelection,
    Event,
    Market,
    OddsQuote,
    Selection,
)

logger = logging.getLogger(__name__)


async def write_markets(
    session: AsyncSession, event: Event, market_specs: Iterable[MarketSpec]
) -> int:
    """Upsert markets / selections / per-book quotes for one event.

    Returns the count of selections written.
    """
    written = 0
    for mspec in market_specs:
        market = await _upsert_market(session, event, mspec)
        for sspec in mspec.selections:
            sel = await _upsert_selection(session, market, sspec)
            written += 1
            await _maybe_record_quote(session, sel, sspec)
            await _upsert_bookmaker_quotes(session, sel, sspec.by_bookmaker)
    return written


# ---- market ----------------------------------------------------------------


async def _resolve_subject_team_id(event: Event, side_marker: str) -> str | None:
    if side_marker == "HOME":
        return event.home_team_id
    if side_marker == "AWAY":
        return event.away_team_id
    return None


async def _upsert_market(
    session: AsyncSession, event: Event, spec: MarketSpec
) -> Market:
    subject_team_id = await _resolve_subject_team_id(event, spec.side)
    row = await session.get(Market, spec.market_id)
    payload = dict(
        event_id=event.id,
        sport_id=event.sport_id,
        category=spec.category,
        type=spec.market_type[:64],
        scope=spec.scope,
        line=spec.line,
        side=spec.side,
        provider="sportsgameodds",
        provider_market_id=(spec.provider_market_id or "")[:128],
        provider_choice_group="",
        subject_team_id=subject_team_id,
        is_live=spec.is_live,
        suspended=spec.suspended,
        last_updated=spec.last_updated_at,
    )
    if row is None:
        row = Market(id=spec.market_id, **payload)
        session.add(row)
    else:
        for k, v in payload.items():
            setattr(row, k, v)
    return row


# ---- selection -------------------------------------------------------------


def _movement(opening: Decimal | None, current: Decimal | None) -> int:
    if opening is None or current is None:
        return 0
    if current > opening:
        return 1
    if current < opening:
        return -1
    return 0


async def _upsert_selection(
    session: AsyncSession, market: Market, spec: SelectionSpec
) -> Selection:
    row = await session.get(Selection, spec.selection_id)
    payload = dict(
        market_id=market.id,
        type=spec.selection_type,
        label=spec.label[:128],
        decimal_odds=spec.decimal_odds,
        opening_decimal_odds=spec.opening_decimal_odds,
        movement=_movement(spec.opening_decimal_odds, spec.decimal_odds),
        suspended=spec.suspended,
    )
    if row is None:
        row = Selection(id=spec.selection_id, **payload)
        session.add(row)
    else:
        for k, v in payload.items():
            setattr(row, k, v)
    # Flush so the FK is satisfied for any rows inserted next (OddsQuote,
    # BookmakerSelection) before we get to the end of the parent transaction.
    await session.flush()
    return row


async def _maybe_record_quote(
    session: AsyncSession, sel: Selection, spec: SelectionSpec
) -> None:
    """One movement row per real price change. Latest price denormalized on
    ``Selection.decimal_odds`` already covers list reads — this is the
    time-series."""
    if spec.decimal_odds is None:
        return
    last = await session.scalar(
        select(OddsQuote.decimal_odds)
        .where(OddsQuote.selection_id == sel.id)
        .order_by(OddsQuote.captured_at.desc())
        .limit(1)
    )
    if last is not None and last == spec.decimal_odds:
        return
    captured_at = datetime.now(tz=timezone.utc)
    # Avoid the unique (selection_id, captured_at) collision on rapid retries.
    existing = await session.scalar(
        select(OddsQuote)
        .where(OddsQuote.selection_id == sel.id, OddsQuote.captured_at == captured_at)
    )
    if existing is None:
        session.add(OddsQuote(
            selection_id=sel.id,
            decimal_odds=spec.decimal_odds,
            captured_at=captured_at,
        ))


async def _upsert_bookmaker_quotes(
    session: AsyncSession,
    sel: Selection,
    books: Iterable[BookmakerQuoteSpec],
) -> None:
    """Bulk upsert all per-bookmaker quotes for one selection.

    Old shape: 2 DB roundtrips per quote (``session.get(Bookmaker)`` +
    ``select(BookmakerSelection).where(...)``). For ~10 bookmakers per
    selection × ~100 selections per event × ~50 events per league, that's
    ~100,000 sequential roundtrips per league walk — at Neon's ~30ms RTT,
    several minutes of pure latency.

    New shape: 2 DB statements TOTAL per selection — one bulk INSERT
    for unknown bookmakers (``ON CONFLICT DO NOTHING``) and one bulk
    INSERT for the per-book selection rows
    (``ON CONFLICT (selection_id, bookmaker_id) DO UPDATE``). The unique
    constraint on the second is declared on the model — see
    ``BookmakerSelection.__table_args__``.
    """
    quotes = list(books)
    if not quotes:
        return

    now = datetime.now(tz=timezone.utc)

    # 1. Make sure every referenced bookmaker exists. ``ON CONFLICT DO
    #    NOTHING`` lets us blast through known + unknown books in one
    #    statement without a pre-flight SELECT. ``id.title()`` is the
    #    same fallback name we used in the old per-quote path.
    bookmaker_rows = [
        {"id": q.bookmaker_id, "name": q.bookmaker_id.title(), "active": True}
        for q in quotes
        if q.bookmaker_id  # skip malformed payloads
    ]
    if bookmaker_rows:
        await session.execute(
            pg_insert(Bookmaker)
            .values(bookmaker_rows)
            .on_conflict_do_nothing(index_elements=["id"])
        )

    # 2. Upsert the per-book selection rows. ``EXCLUDED.<col>`` references
    #    the row that *would have* been inserted, which is how we get
    #    proper UPDATE semantics (overwrite price + spread + last_updated_at).
    selection_rows = [
        {
            "selection_id": sel.id,
            "bookmaker_id": q.bookmaker_id,
            "decimal_odds": q.decimal_odds,
            "spread": q.spread,
            "over_under": q.over_under,
            "available": q.available,
            "deeplink": q.deeplink or "",
            "last_updated_at": q.last_updated_at or now,
        }
        for q in quotes
        if q.bookmaker_id
    ]
    if not selection_rows:
        return
    stmt = pg_insert(BookmakerSelection).values(selection_rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["selection_id", "bookmaker_id"],
        set_={
            "decimal_odds": stmt.excluded.decimal_odds,
            "spread": stmt.excluded.spread,
            "over_under": stmt.excluded.over_under,
            "available": stmt.excluded.available,
            "deeplink": stmt.excluded.deeplink,
            "last_updated_at": stmt.excluded.last_updated_at,
        },
    )
    await session.execute(stmt)
