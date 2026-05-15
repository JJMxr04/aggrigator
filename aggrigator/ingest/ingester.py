"""Persist normalized ``MarketSpec`` lists to the DB — bulk-upsert path.

Hot path of the ingest pipeline. Each ``write_markets`` call used to issue
~30-40 Postgres roundtrips per event:

  - ``session.get(Market)`` then INSERT/UPDATE per market   (~3-6 RTT)
  - ``session.get(Selection)`` + INSERT/UPDATE + flush     (~6-18 RTT)
  - SELECT last OddsQuote + maybe INSERT                   (~12 RTT)
  - bulk INSERT Bookmaker + BookmakerSelection             (~2 RTT)

With a typical 30-event MLB walk that's >1000 roundtrips/cycle.

Rewrite (May 2026): collapse each kind of write into a single bulk
``INSERT … ON CONFLICT DO UPDATE`` statement per event. Per-event cost
drops to ~4-5 RTT regardless of selection count.

Idempotency: every upsert uses ``pg_insert(...).on_conflict_do_update(...)``
keyed on the deterministic IDs ``normalize.py`` produces. Re-running on
the same payload converges to the same rows; no duplicates, no
double-INSERT exceptions.
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


def _movement(opening: Decimal | None, current: Decimal | None) -> int:
    if opening is None or current is None:
        return 0
    if current > opening:
        return 1
    if current < opening:
        return -1
    return 0


def _resolve_subject_team_id(event: Event, side_marker: str) -> str | None:
    if side_marker == "HOME":
        return event.home_team_id
    if side_marker == "AWAY":
        return event.away_team_id
    return None


async def write_markets(
    session: AsyncSession, event: Event, market_specs: Iterable[MarketSpec],
) -> int:
    """Upsert markets / selections / per-book quotes for one event.

    Returns the count of selections written.

    Bulk pattern: one INSERT statement per kind of row, regardless of how
    many markets/selections the event carries. The total Postgres RTTs
    are ~5 per event (existing-selection lookup + 4 INSERTs) instead of
    one per selection.
    """
    specs = list(market_specs)
    if not specs:
        return 0

    now = datetime.now(tz=timezone.utc)
    all_selection_specs: list[tuple[MarketSpec, SelectionSpec]] = [
        (m, s) for m in specs for s in m.selections
    ]
    if not all_selection_specs:
        return 0

    # 1. Prefetch existing selection prices so we can skip OddsQuote inserts
    #    when the price hasn't moved. One SELECT replaces N per-selection
    #    "last OddsQuote" lookups.
    selection_ids = [s.selection_id for _, s in all_selection_specs]
    prior_prices: dict[str, Decimal | None] = dict(
        (
            await session.execute(
                select(Selection.id, Selection.decimal_odds).where(
                    Selection.id.in_(selection_ids)
                )
            )
        ).all()
    )

    # 2. Bulk upsert markets
    await _bulk_upsert_markets(session, event, specs, now)

    # 3. Bulk upsert selections
    await _bulk_upsert_selections(session, all_selection_specs)

    # 4. Bulk insert OddsQuote rows ONLY for selections whose price changed
    quote_rows: list[dict] = []
    for _, sspec in all_selection_specs:
        if sspec.decimal_odds is None:
            continue
        if prior_prices.get(sspec.selection_id) == sspec.decimal_odds:
            continue
        quote_rows.append({
            "selection_id": sspec.selection_id,
            "decimal_odds": sspec.decimal_odds,
            "captured_at": now,
        })
    if quote_rows:
        await session.execute(
            pg_insert(OddsQuote)
            .values(quote_rows)
            .on_conflict_do_nothing(
                index_elements=["selection_id", "captured_at"],
            )
        )

    # 5. Bulk upsert bookmakers + bookmaker_selections (per-book quotes)
    await _bulk_upsert_book_quotes(session, all_selection_specs, now)

    return len(all_selection_specs)


# ---- bulk upsert helpers ----------------------------------------------------


async def _bulk_upsert_markets(
    session: AsyncSession,
    event: Event,
    specs: Iterable[MarketSpec],
    now: datetime,
) -> None:
    rows = []
    for spec in specs:
        rows.append({
            "id": spec.market_id,
            "event_id": event.id,
            "sport_id": event.sport_id,
            "category": spec.category,
            "type": spec.market_type[:64],
            "scope": spec.scope,
            "line": spec.line,
            "side": spec.side,
            "provider": "oddsapi",
            "provider_market_id": (spec.provider_market_id or "")[:128],
            "provider_choice_group": "",
            "subject_team_id": _resolve_subject_team_id(event, spec.side),
            "is_live": spec.is_live,
            "suspended": spec.suspended,
            "last_updated": spec.last_updated_at,
        })
    if not rows:
        return
    stmt = pg_insert(Market).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "event_id": stmt.excluded.event_id,
            "sport_id": stmt.excluded.sport_id,
            "category": stmt.excluded.category,
            "type": stmt.excluded.type,
            "scope": stmt.excluded.scope,
            "line": stmt.excluded.line,
            "side": stmt.excluded.side,
            "provider": stmt.excluded.provider,
            "provider_market_id": stmt.excluded.provider_market_id,
            "provider_choice_group": stmt.excluded.provider_choice_group,
            "subject_team_id": stmt.excluded.subject_team_id,
            "is_live": stmt.excluded.is_live,
            "suspended": stmt.excluded.suspended,
            "last_updated": stmt.excluded.last_updated,
        },
    )
    await session.execute(stmt)


async def _bulk_upsert_selections(
    session: AsyncSession,
    pairs: list[tuple[MarketSpec, SelectionSpec]],
) -> None:
    rows = []
    for _, sspec in pairs:
        rows.append({
            "id": sspec.selection_id,
            "market_id": sspec.market_id,
            "type": sspec.selection_type,
            "label": sspec.label[:128],
            "decimal_odds": sspec.decimal_odds,
            "opening_decimal_odds": sspec.opening_decimal_odds,
            "movement": _movement(sspec.opening_decimal_odds, sspec.decimal_odds),
            "suspended": sspec.suspended,
        })
    if not rows:
        return
    stmt = pg_insert(Selection).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "market_id": stmt.excluded.market_id,
            "type": stmt.excluded.type,
            "label": stmt.excluded.label,
            "decimal_odds": stmt.excluded.decimal_odds,
            "opening_decimal_odds": stmt.excluded.opening_decimal_odds,
            "movement": stmt.excluded.movement,
            "suspended": stmt.excluded.suspended,
        },
    )
    await session.execute(stmt)


async def _bulk_upsert_book_quotes(
    session: AsyncSession,
    pairs: list[tuple[MarketSpec, SelectionSpec]],
    now: datetime,
) -> None:
    """Flatten per-bookmaker quotes from every selection into two bulk
    statements: one for the Bookmaker rows (ON CONFLICT DO NOTHING) and
    one for the BookmakerSelection rows (ON CONFLICT DO UPDATE).
    """
    book_rows: dict[str, dict] = {}
    selection_book_rows: list[dict] = []
    for _, sspec in pairs:
        for q in sspec.by_bookmaker:
            if not q.bookmaker_id:
                continue
            book_rows.setdefault(q.bookmaker_id, {
                "id": q.bookmaker_id,
                "name": q.bookmaker_id.title(),
                "active": True,
            })
            selection_book_rows.append({
                "selection_id": sspec.selection_id,
                "bookmaker_id": q.bookmaker_id,
                "decimal_odds": q.decimal_odds,
                "spread": q.spread,
                "over_under": q.over_under,
                "available": q.available,
                "deeplink": q.deeplink or "",
                "last_updated_at": q.last_updated_at or now,
            })

    if book_rows:
        await session.execute(
            pg_insert(Bookmaker)
            .values(list(book_rows.values()))
            .on_conflict_do_nothing(index_elements=["id"])
        )
    if selection_book_rows:
        stmt = pg_insert(BookmakerSelection).values(selection_book_rows)
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
