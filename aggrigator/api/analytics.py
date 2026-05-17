"""Analytics reads for the MDProject portal — ``/v1/analytics/*``.

Four endpoints consumed by ``core/portal/services/aggrigator_client.py``:

- ``GET /v1/analytics/events/today`` — denormalized Tonight list
  (event + ML/spread/total summary) for the next N hours.
- ``GET /v1/analytics/events/{event_id}/probabilities`` — implied + vig-free
  probabilities per market for one event.
- ``GET /v1/analytics/events/{event_id}/best-prices`` — per-book quotes
  with the highest-decimal book flagged ``is_best``.
- ``GET /v1/analytics/disagreements`` — cross-book spread leaderboard.

Schemas are intentionally loose (dict / list) because the client treats
them as JSON — see ``portal/templates/portal/analytics/*.html`` for the
field names the templates render.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from aggrigator.deps import SessionDep
from aggrigator.models import (
    BookmakerSelection,
    Event,
    Market,
    MarketCategory,
    MarketScope,
    Selection,
)

router = APIRouter(prefix="/v1/analytics", tags=["analytics"])


_SUMMARY_CATEGORIES = (
    MarketCategory.MONEYLINE.value,
    MarketCategory.SPREAD.value,
    MarketCategory.TOTAL.value,
)
_CATEGORY_KEY = {
    MarketCategory.MONEYLINE.value: "ml",
    MarketCategory.SPREAD.value: "spread",
    MarketCategory.TOTAL.value: "total",
}


def _team_dto(team) -> dict | None:
    """Match ``TeamOut`` from ``/v1/events/{id}`` so the Tonight list and
    Tonight detail render the same team string for the same team."""
    if team is None:
        return None
    return {
        "team_id": team.team_id,
        "name": team.name_long,
        "name_medium": team.name_medium,
        "name_short": team.name_short,
        "logo_url": team.logo_url,
    }


def _implied_prob(odds: Decimal | None) -> float | None:
    if odds is None or odds == 0:
        return None
    return float(Decimal(1) / odds)


def _summary_market_dto(m: Market) -> dict:
    """Compact view used by the Tonight list — just type, line, decimal_odds,
    implied_prob. Per-book detail is reserved for the detail endpoints."""
    selections: list[dict] = []
    for sel in m.selections:
        selections.append({
            "type": sel.type,
            "label": sel.label,
            "decimal_odds": (
                float(sel.decimal_odds) if sel.decimal_odds is not None else None
            ),
            "implied_prob": _implied_prob(sel.decimal_odds),
        })
    return {
        "market_id": m.id,
        "line": float(m.line) if m.line is not None else None,
        "selections": selections,
    }


@router.get("/events/today")
async def events_today(
    session: SessionDep,
    hours_ahead: int = Query(default=24, ge=1, le=168),
    sport: str | None = Query(default=None),
    league: str | None = Query(default=None),
) -> list[dict]:
    """Events kicking off within the next ``hours_ahead`` hours, each
    enriched with ML/spread/total FULL_GAME markets."""
    now = datetime.now(tz=timezone.utc)
    window_end = now + timedelta(hours=hours_ahead)

    stmt = (
        select(Event)
        .where(
            Event.start_time >= now - timedelta(hours=3),
            Event.start_time < window_end,
        )
        .options(
            selectinload(Event.home_team),
            selectinload(Event.away_team),
            selectinload(Event.sport),
            selectinload(Event.league),
        )
        .order_by(Event.start_time)
    )
    if sport:
        stmt = stmt.where(Event.sport_id == sport)
    if league:
        stmt = stmt.where(Event.league_id == league)

    events = list(await session.scalars(stmt))
    if not events:
        return []

    event_ids = [e.id for e in events]
    markets_stmt = (
        select(Market)
        .where(
            Market.event_id.in_(event_ids),
            Market.category.in_(_SUMMARY_CATEGORIES),
            Market.scope == MarketScope.FULL_GAME.value,
        )
        .options(selectinload(Market.selections))
        .order_by(Market.event_id, Market.category, Market.line)
    )
    summary: dict[str, dict[str, Market]] = {}
    for m in await session.scalars(markets_stmt):
        # First market per (event, category) wins — the deterministic
        # ordering above keeps this stable across requests.
        summary.setdefault(m.event_id, {}).setdefault(m.category, m)

    out: list[dict] = []
    for ev in events:
        row: dict = {
            "event_id": ev.id,
            "start_time": ev.start_time,
            "status_type": ev.status_type,
            "status_display": ev.status_display,
            "sport_id": ev.sport_id,
            "league_id": ev.league_id,
            "home_team": _team_dto(ev.home_team),
            "away_team": _team_dto(ev.away_team),
            "home_score": ev.home_score,
            "away_score": ev.away_score,
            "ml": None,
            "spread": None,
            "total": None,
        }
        for cat, market in summary.get(ev.id, {}).items():
            row[_CATEGORY_KEY[cat]] = _summary_market_dto(market)
        out.append(row)
    return out


@router.get("/events/{event_id}/probabilities")
async def event_probabilities(session: SessionDep, event_id: str) -> dict:
    """Vig-adjusted implied probabilities per market for one event.

    ``raw`` = 1 / decimal_odds. ``normalized`` = raw / sum(raw) within the
    market — this is the vig-free fair probability. ``vig`` = sum(raw) - 1
    (the bookmaker's hold expressed as overround above 1.0)."""
    event = await session.get(Event, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")

    stmt = (
        select(Market)
        .where(Market.event_id == event_id)
        .options(selectinload(Market.selections))
        .order_by(Market.category, Market.scope, Market.line)
    )
    markets_out: list[dict] = []
    for m in await session.scalars(stmt):
        raws = [(s, _implied_prob(s.decimal_odds)) for s in m.selections]
        priced = [(s, r) for s, r in raws if r is not None]
        total_raw = sum(r for _, r in priced) if priced else 0.0
        vig = total_raw - 1.0 if total_raw > 0 else None
        selections_out: list[dict] = []
        for sel, raw in raws:
            normalized = (raw / total_raw) if (raw is not None and total_raw > 0) else None
            selections_out.append({
                "type": sel.type,
                "label": sel.label,
                "decimal_odds": (
                    float(sel.decimal_odds) if sel.decimal_odds is not None else None
                ),
                "implied_prob": raw,
                "normalized": normalized,
            })
        markets_out.append({
            "market_id": m.id,
            "category": m.category,
            "scope": m.scope,
            "type": m.type,
            "line": float(m.line) if m.line is not None else None,
            "vig": vig,
            "selections": selections_out,
        })

    return {"event_id": event_id, "markets": markets_out}


@router.get("/events/{event_id}/best-prices")
async def event_best_prices(session: SessionDep, event_id: str) -> dict:
    """Per-book quote table for an event. ``consensus_decimal_odds`` is the
    canonical ``Selection.decimal_odds`` (already a consensus across books at
    ingest time); ``is_best`` flags the bookmaker offering the highest
    decimal price for that selection — i.e. the best payout for the bettor."""
    event = await session.get(Event, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")

    stmt = (
        select(Selection)
        .join(Market, Market.id == Selection.market_id)
        .where(Market.event_id == event_id)
        .options(
            selectinload(Selection.market),
            selectinload(Selection.by_book).selectinload(BookmakerSelection.bookmaker),
        )
        .order_by(Market.category, Market.scope, Market.line, Selection.type)
    )

    selections_out: list[dict] = []
    for sel in await session.scalars(stmt):
        books_raw = [b for b in sel.by_book if b.available and b.decimal_odds is not None]
        best_id: int | None = None
        if books_raw:
            best = max(books_raw, key=lambda b: b.decimal_odds)
            best_id = best.id
        books_out: list[dict] = []
        for b in sel.by_book:
            books_out.append({
                "bookmaker_id": b.bookmaker_id,
                "bookmaker_name": b.bookmaker.name if b.bookmaker else None,
                "decimal_odds": (
                    float(b.decimal_odds) if b.decimal_odds is not None else None
                ),
                "available": b.available,
                "is_best": b.id == best_id,
                "deeplink": b.deeplink or None,
            })
        selections_out.append({
            "selection_id": sel.id,
            "market_id": sel.market_id,
            "market_category": sel.market.category if sel.market else None,
            "type": sel.type,
            "label": sel.label,
            "consensus_decimal_odds": (
                float(sel.decimal_odds) if sel.decimal_odds is not None else None
            ),
            "books": books_out,
        })

    return {"event_id": event_id, "selections": selections_out}


@router.get("/disagreements")
async def disagreements(
    session: SessionDep,
    threshold_pct: float = Query(default=2.0, ge=0.0, le=100.0),
    hours_ahead: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=25, ge=1, le=200),
) -> dict:
    """Selections whose cross-book min/max decimal odds differ by at least
    ``threshold_pct`` percent, sorted by spread desc. Spread is
    ``(max - min) / min * 100`` — bookie payout differential as a percent
    of the worst price."""
    now = datetime.now(tz=timezone.utc)
    window_end = now + timedelta(hours=hours_ahead)

    stmt = (
        select(Selection)
        .join(Market, Market.id == Selection.market_id)
        .join(Event, Event.id == Market.event_id)
        .where(
            Event.start_time >= now - timedelta(hours=3),
            Event.start_time < window_end,
            Selection.settlement_status == "PENDING",
        )
        .options(
            selectinload(Selection.market),
            selectinload(Selection.by_book).selectinload(BookmakerSelection.bookmaker),
        )
    )

    rows: list[dict] = []
    for sel in await session.scalars(stmt):
        priced = [
            b for b in sel.by_book
            if b.available and b.decimal_odds is not None and b.decimal_odds > 0
        ]
        if len(priced) < 2:
            continue
        lo = min(priced, key=lambda b: b.decimal_odds)
        hi = max(priced, key=lambda b: b.decimal_odds)
        if lo.decimal_odds == 0:
            continue
        spread_pct = float((hi.decimal_odds - lo.decimal_odds) / lo.decimal_odds) * 100.0
        if spread_pct < threshold_pct:
            continue
        market = sel.market
        rows.append({
            "event_id": market.event_id if market else None,
            "selection_id": sel.id,
            "market_id": sel.market_id,
            "market_category": market.category if market else None,
            "market_line": (
                float(market.line) if market and market.line is not None else None
            ),
            "type": sel.type,
            "label": sel.label,
            "min_book": (lo.bookmaker.name if lo.bookmaker else lo.bookmaker_id),
            "min_decimal_odds": float(lo.decimal_odds),
            "max_book": (hi.bookmaker.name if hi.bookmaker else hi.bookmaker_id),
            "max_decimal_odds": float(hi.decimal_odds),
            "spread_pct": spread_pct,
        })

    rows.sort(key=lambda r: r["spread_pct"], reverse=True)
    return {"rows": rows[:limit], "threshold_pct": threshold_pct}
