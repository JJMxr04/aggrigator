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

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from aggrigator.analytics import soccer_model
from aggrigator.deps import SessionDep, require_pro_user
from aggrigator.models import (
    BookmakerSelection,
    Event,
    League,
    Market,
    MarketCategory,
    MarketScope,
    Selection,
    Team,
)

# Every route on this router requires a valid PRO tenant API key. Applying
# at the router level means a future contributor adding an endpoint can't
# forget the gate. See subscription-plan/05-access-control.md §2.
router = APIRouter(
    prefix="/v1/analytics",
    tags=["analytics"],
    dependencies=[Depends(require_pro_user)],
)


def _team_dto(team) -> dict | None:
    """Compact team payload used by the events / fixtures endpoints. ``id``
    is the synthesized ``{league_id}:{team_id}`` PK that the explore
    pages use for drill-in URLs; ``team_id`` is the raw provider id
    kept for legacy callers."""
    if team is None:
        return None
    return {
        "id": team.id,
        "team_id": team.team_id,
        "league_id": team.league_id,
        "name": team.name_long,
        "name_medium": team.name_medium,
        "name_short": team.name_short,
        "logo_url": team.logo_url,
    }


def _implied_prob(odds: Decimal | None) -> float | None:
    if odds is None or odds == 0:
        return None
    return float(Decimal(1) / odds)


@router.get("/events/today")
async def events_today(
    session: SessionDep,
    hours_ahead: int = Query(default=72, ge=1, le=168),
    league: str | None = Query(default=None),
) -> dict:
    """Upcoming events with model probability + live odds + best edge.

    Default window widened to 72 h (was 24 h in the pre-redesign shape
    — see ``plans/analytics/dashboard_and_data/06-upcoming-and-picks.md``).
    Past events are excluded (``is_finalized=False``)."""
    rows, window = await _upcoming_rows(session, hours_ahead=hours_ahead, league=league)
    return {"from": window[0], "to": window[1], "events": rows}


@router.get("/picks")
async def picks(
    session: SessionDep,
    threshold_pp: float = Query(default=3.0, ge=0.0, le=100.0),
    hours_ahead: int = Query(default=72, ge=1, le=168),
    league: str | None = Query(default=None),
) -> dict:
    """Same upcoming list filtered to rows whose best_edge clears the
    threshold (percentage points). Sorted edge DESC."""
    rows, window = await _upcoming_rows(session, hours_ahead=hours_ahead, league=league)
    picks = [
        r for r in rows
        if r.get("best_edge") and r["best_edge"]["edge_pp"] >= threshold_pp
    ]
    picks.sort(key=lambda r: r["best_edge"]["edge_pp"], reverse=True)
    return {
        "from": window[0],
        "to": window[1],
        "threshold_pp": threshold_pp,
        "events": picks,
    }


async def _upcoming_rows(
    session,
    *,
    hours_ahead: int,
    league: str | None,
) -> tuple[list[dict], tuple[datetime, datetime]]:
    """Shared backbone for ``events_today`` + ``picks``.

    Pulls upcoming events in window, computes model probabilities once
    per league (so 10 EPL events don't pay 10× the Elo walk), reads
    live-odds from ``core_market`` in a single batched query, and
    builds the dashboard row shape with ``best_edge`` joined."""
    now = datetime.now(tz=timezone.utc)
    window_end = now + timedelta(hours=hours_ahead)

    stmt = (
        select(Event)
        .where(
            Event.start_time >= now,
            Event.start_time < window_end,
            Event.is_finalized.is_(False),
        )
        .options(
            selectinload(Event.home_team),
            selectinload(Event.away_team),
            selectinload(Event.sport),
            selectinload(Event.league),
        )
        .order_by(Event.start_time)
    )
    if league:
        stmt = stmt.where(Event.league_id == league)

    events = list(await session.scalars(stmt))
    if not events:
        return [], (now, window_end)

    # ---- Per-league model state (computed once each) ----
    elo_state_by_league, base_goals_by_league = await _league_model_states(
        session, leagues={ev.league_id for ev in events if ev.league_id and ev.sport_id == "SOCCER"},
    )

    # ---- Live odds for every event in one bulk pull ----
    event_ids = [ev.id for ev in events]
    markets_stmt = (
        select(Market)
        .where(
            Market.event_id.in_(event_ids),
            Market.category == MarketCategory.MONEYLINE.value,
            Market.scope == MarketScope.FULL_GAME.value,
        )
        .options(
            selectinload(Market.selections).selectinload(Selection.by_book)
            .selectinload(BookmakerSelection.bookmaker),
        )
        .order_by(Market.event_id, Market.line)
    )
    markets_by_event: dict[str, list[Market]] = {}
    for m in await session.scalars(markets_stmt):
        markets_by_event.setdefault(m.event_id, []).append(m)

    rows: list[dict] = []
    for ev in events:
        model_prob = None
        if ev.sport_id == "SOCCER" and ev.league_id in elo_state_by_league:
            state = elo_state_by_league[ev.league_id]
            base_home, base_away = base_goals_by_league[ev.league_id]
            if ev.home_team_id and ev.away_team_id:
                home_elo, away_elo = soccer_model.elo_snapshot_at(
                    state, None, ev.home_team_id, ev.away_team_id,
                )
                probs = soccer_model.compute_match_probabilities(
                    home_elo, away_elo, base_home=base_home, base_away=base_away,
                )
                model_prob = {
                    "p_home": probs["p_home"],
                    "p_draw": probs["p_draw"],
                    "p_away": probs["p_away"],
                    "model_version": soccer_model.MODEL_VERSION,
                }

        live_odds_payload = _build_live_odds_payload(
            ev.id, markets_by_event.get(ev.id, []), fetched_at=now,
        )
        has_live_odds = bool(live_odds_payload.get("markets"))
        best_edge = _best_edge_from_live_odds(live_odds_payload, model_prob or {})

        rows.append({
            "event_id": ev.id,
            "start_time": ev.start_time,
            "status_type": ev.status_type,
            "status_display": ev.status_display,
            "sport_id": ev.sport_id,
            "league_id": ev.league_id,
            "home_team": _team_dto(ev.home_team),
            "away_team": _team_dto(ev.away_team),
            "model_prob": model_prob,
            "has_live_odds": has_live_odds,
            "best_edge": best_edge,
        })
    return rows, (now, window_end)


async def _league_model_states(
    session, leagues: set[str | None],
) -> tuple[dict, dict]:
    """Build ``{league_id: EloLeagueState}`` and ``{league_id: (base_h, base_a)}``.

    One SELECT per league for its settled events. Caller drops any
    leagues outside soccer; if a league has zero settled events it's
    silently absent from the result (the caller treats absence as
    "no model available for this row")."""
    elo_by_league: dict = {}
    bases_by_league: dict = {}
    for league_id in leagues:
        if not league_id:
            continue
        league_events = list(await session.scalars(
            select(Event)
            .where(
                Event.league_id == league_id,
                Event.is_finalized.is_(True),
            )
            .order_by(Event.start_time.asc(), Event.id)
        ))
        if not league_events:
            continue
        elo_by_league[league_id] = soccer_model.compute_elo_for_league(league_events)
        bases_by_league[league_id] = soccer_model.league_average_goals(league_events)
    return elo_by_league, bases_by_league


def _build_live_odds_payload(
    event_id: str, markets: list[Market], *, fetched_at: datetime,
) -> dict:
    """Pure builder — used by the live-odds endpoint and the upcoming list.

    ``markets`` is the moneyline FULL_GAME markets for one event, with
    selections + by_book + bookmaker eager-loaded. Returns the same
    shape as the ``/live-odds`` endpoint so consumers can treat them
    interchangeably."""
    if not markets:
        return {
            "event_id": event_id,
            "fetched_at": fetched_at,
            "markets": [],
            "reason": "no_markets_for_event",
        }

    markets_out: list[dict] = []
    for m in markets:
        best_prices: list[dict] = []
        raw_implied: dict[str, float] = {}
        for sel in m.selections:
            available_books = [
                b for b in sel.by_book
                if b.available and b.decimal_odds is not None and b.decimal_odds > 0
            ]
            if not available_books:
                continue
            best = max(available_books, key=lambda b: b.decimal_odds)
            side = _selection_side(sel.type)
            implied = _implied_prob(best.decimal_odds)
            best_prices.append({
                "side": side,
                "selection_type": sel.type,
                "label": sel.label,
                "bookmaker_id": best.bookmaker_id,
                "bookmaker_name": best.bookmaker.name if best.bookmaker else best.bookmaker_id,
                "decimal_odds": float(best.decimal_odds),
                "implied_prob": implied,
                "deeplink": best.deeplink or None,
            })
            if side in {"home", "draw", "away"} and implied is not None:
                raw_implied[side] = implied

        if not best_prices:
            continue

        total = sum(raw_implied.values())
        vig_adjusted: dict[str, float] = {}
        if total > 0:
            for side, val in raw_implied.items():
                vig_adjusted[side] = val / total

        markets_out.append({
            "market_id": m.id,
            "market_type": "moneyline",
            "last_updated": m.last_updated,
            "best_prices": best_prices,
            "vig_adjusted_implied": vig_adjusted,
            "vig": (total - 1.0) if total > 0 else None,
        })

    return {
        "event_id": event_id,
        "fetched_at": fetched_at,
        "markets": markets_out,
        "reason": None if markets_out else "no_books_cover_event",
    }


@router.get("/events/{event_id}/probabilities")
async def event_probabilities(session: SessionDep, event_id: str) -> dict:
    """Model-derived match probabilities for one event.

    Replaces the previous market-implied shape (per the 2026-05-19
    redesign — see ``plans/analytics/dashboard_and_data/00-overview.md``).
    The model is soccer-specific (Elo + bivariate Poisson — see
    ``aggrigator/analytics/soccer_model.py``); non-soccer events return
    ``model_version=null`` and null probabilities until their pipelines
    land.

    For **past events**, returns the *pre-match* estimate computed from
    Elo ratings as of strictly before kickoff — the question this
    answers is "what would the model have said at the time," which is
    the only honest framing for a post-hoc display.

    For **future events**, returns the *current* estimate using the most
    recent ratings.
    """
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

    empty = {
        "event_id": event_id,
        "p_home": None,
        "p_draw": None,
        "p_away": None,
        "model_version": None,
        "computed_at": None,
        "reason": None,
    }

    sport_id = event.sport_id or (event.sport.id if event.sport else None)
    if sport_id != "SOCCER":
        return {**empty, "reason": "no_model_for_sport"}

    if event.league_id is None or event.home_team_id is None or event.away_team_id is None:
        return {**empty, "reason": "insufficient_event_context"}

    league_events = list(await session.scalars(
        select(Event)
        .where(
            Event.league_id == event.league_id,
            Event.is_finalized.is_(True),
        )
        .order_by(Event.start_time.asc(), Event.id)
    ))

    if not league_events:
        # No history → can't compute ratings; the team-strength estimate
        # would just be the league average. Return null rather than a
        # misleading point estimate.
        return {**empty, "reason": "no_settled_events_in_league"}

    state = soccer_model.compute_elo_for_league(league_events)
    home_elo, away_elo = soccer_model.elo_snapshot_at(
        state, event.id if event.is_finalized else None,
        event.home_team_id, event.away_team_id,
    )
    base_home, base_away = soccer_model.league_average_goals(league_events)
    probs = soccer_model.compute_match_probabilities(
        home_elo, away_elo, base_home=base_home, base_away=base_away,
    )

    return {
        "event_id": event_id,
        "p_home": probs["p_home"],
        "p_draw": probs["p_draw"],
        "p_away": probs["p_away"],
        "model_version": soccer_model.MODEL_VERSION,
        "computed_at": datetime.now(tz=timezone.utc),
        "model_inputs": {
            "home_elo": home_elo,
            "away_elo": away_elo,
            "base_home_goals": base_home,
            "base_away_goals": base_away,
            "mu_home": probs["mu_home"],
            "mu_away": probs["mu_away"],
            "n_events_in_league": len(league_events),
        },
        "is_pre_match_snapshot": event.is_finalized,
    }


@router.get("/events/{event_id}/live-odds")
async def event_live_odds(session: SessionDep, event_id: str) -> dict:
    """Best price per side + vig-adjusted implied probabilities for one event.

    Reads from the existing market tables (``core_market`` +
    ``core_selection`` + ``core_bookmaker_selection``) that the ingest
    cron writes. v1 surfaces moneyline only; spread/total are reserved
    for a future expansion.

    Returns ``{markets: []}`` with a ``reason`` code when no coverage is
    available — never raises 404 so the dashboard's empty-state copy
    can branch on the reason instead of HTTP status. A genuinely
    missing event is the only 404."""
    event = await session.get(Event, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")

    fetched_at = datetime.now(tz=timezone.utc)

    if event.is_finalized:
        return {
            "event_id": event_id,
            "fetched_at": fetched_at,
            "markets": [],
            "reason": "event_is_finalized",
        }

    markets = list(await session.scalars(
        select(Market)
        .where(
            Market.event_id == event_id,
            Market.category == MarketCategory.MONEYLINE.value,
            Market.scope == MarketScope.FULL_GAME.value,
        )
        .options(
            selectinload(Market.selections).selectinload(Selection.by_book)
            .selectinload(BookmakerSelection.bookmaker),
        )
        .order_by(Market.line)
    ))

    return _build_live_odds_payload(event_id, markets, fetched_at=fetched_at)


def _selection_side(selection_type: str) -> str:
    """Map ``Selection.type`` to the dashboard's side vocabulary."""
    t = (selection_type or "").upper()
    if t == "HOME":
        return "home"
    if t == "AWAY":
        return "away"
    if t == "DRAW":
        return "draw"
    return t.lower()


def _best_edge_from_live_odds(
    live_odds_payload: dict, model_prob: dict,
) -> dict | None:
    """Pick the highest +edge side from the model vs market comparison.

    Returns ``{side, edge_pp, model_prob, market_prob, best_decimal,
    best_bookmaker, best_deeplink}`` or None when there's no overlap.
    Edge is signed; callers filter by the sign / threshold themselves.
    """
    if not live_odds_payload or not model_prob:
        return None
    if model_prob.get("p_home") is None:
        return None
    markets = live_odds_payload.get("markets") or []
    ml = next((m for m in markets if m.get("market_type") == "moneyline"), None)
    if ml is None:
        return None
    vig_adj = ml.get("vig_adjusted_implied") or {}
    if not vig_adj:
        return None
    best_by_side = {
        bp["side"]: bp for bp in ml.get("best_prices", []) if bp.get("side")
    }
    model_by_side = {
        "home": model_prob.get("p_home"),
        "draw": model_prob.get("p_draw"),
        "away": model_prob.get("p_away"),
    }

    candidates: list[dict] = []
    for side, market_p in vig_adj.items():
        model_p = model_by_side.get(side)
        if model_p is None or market_p is None:
            continue
        bp = best_by_side.get(side)
        if bp is None:
            continue
        edge = model_p - market_p
        candidates.append({
            "side": side,
            "edge_pp": edge * 100.0,
            "model_prob": model_p,
            "market_prob": market_p,
            "best_decimal": bp.get("decimal_odds"),
            "best_bookmaker": bp.get("bookmaker_name"),
            "best_deeplink": bp.get("deeplink"),
        })
    if not candidates:
        return None
    candidates.sort(key=lambda c: c["edge_pp"], reverse=True)
    return candidates[0]


@router.get("/events/{event_id}/context")
async def event_context(session: SessionDep, event_id: str) -> dict:
    """Form-into-match for both teams + (for past events) H2H last 5.

    Powers the event detail page's secondary panels. Read-only, no
    cache, recomputed per request from the league's settled events."""
    event = await session.scalar(
        select(Event)
        .where(Event.id == event_id)
        .options(
            selectinload(Event.home_team),
            selectinload(Event.away_team),
        )
    )
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")

    if not event.league_id or not event.home_team_id or not event.away_team_id:
        return {
            "event_id": event_id,
            "form_into_match": {"home_last_5": [], "away_last_5": []},
            "h2h_last_5": [],
        }

    league_events = list(await session.scalars(
        select(Event)
        .where(
            Event.league_id == event.league_id,
            Event.is_finalized.is_(True),
        )
        .options(
            selectinload(Event.home_team),
            selectinload(Event.away_team),
        )
        .order_by(Event.start_time.desc(), Event.id)
    ))

    home_form = soccer_model.compute_form_into_match(
        event.home_team_id, league_events,
        target_start_time=event.start_time, last_n=5,
    )
    away_form = soccer_model.compute_form_into_match(
        event.away_team_id, league_events,
        target_start_time=event.start_time, last_n=5,
    )

    h2h_last_5: list[dict] = []
    # H2H always reflects fixtures strictly before this event so the
    # number means "what their history looked like coming in," whether
    # the user is looking at a past or upcoming match.
    for ev in league_events:
        if event.start_time and ev.start_time and ev.start_time >= event.start_time:
            continue
        if ev.id == event.id:
            continue
        pair_match = (
            (ev.home_team_id == event.home_team_id and ev.away_team_id == event.away_team_id)
            or (ev.home_team_id == event.away_team_id and ev.away_team_id == event.home_team_id)
        )
        if not pair_match:
            continue
        h2h_last_5.append({
            "event_id": ev.id,
            "start_time": ev.start_time,
            "home_team": _team_dto(ev.home_team),
            "away_team": _team_dto(ev.away_team),
            "home_score": ev.home_score,
            "away_score": ev.away_score,
            "winner_code": ev.winner_code,
        })
        if len(h2h_last_5) == 5:
            break

    return {
        "event_id": event_id,
        "form_into_match": {
            "home_last_5": home_form,
            "away_last_5": away_form,
        },
        "h2h_last_5": h2h_last_5,
    }


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


# --- Explore: leagues / fixtures / standings -------------------------------
#
# Score-driven explore views (file 03 of the dashboard plan). These hit
# only ``core_event_event`` + ``core_team`` + ``core_league``; no odds
# tables are consulted. Designed around TheSportsDB's historical scored
# events (``provider='thesportsdb'``) but agnostic to provenance —
# anything ``is_finalized=True`` qualifies.


# Soccer points scheme. NBA/NFL/MLB/NHL will need a sport-conditional
# dispatch when their pipelines land (open question 4 in the dashboard
# plan); v1 hardcodes soccer rules.
_POINTS_WIN = 3
_POINTS_DRAW = 1


@router.get("/leagues")
async def list_leagues(session: SessionDep) -> dict:
    """League catalog with summary counts for the Explore landing grid.

    For each league: ``event_count`` (settled events on disk),
    ``team_count`` (rows in ``core_team``), ``last_event_at`` (latest
    settled kickoff), ``seasons_available`` (distinct season_label
    values present in the events table)."""
    leagues = list(await session.scalars(
        select(League)
        .options(selectinload(League.sport))
        .order_by(League.id)
    ))

    # Settled-event aggregates per league. Joining in a single GROUP BY
    # rather than N+1; leagues table is tiny but the events table is the
    # one big table touched here.
    event_agg_rows = (await session.execute(
        select(
            Event.league_id,
            func.count().label("event_count"),
            func.max(Event.start_time).label("last_event_at"),
        )
        .where(Event.is_finalized.is_(True), Event.league_id.is_not(None))
        .group_by(Event.league_id)
    )).all()
    event_agg: dict[str, dict] = {
        row.league_id: {
            "event_count": int(row.event_count or 0),
            "last_event_at": row.last_event_at,
        }
        for row in event_agg_rows
    }

    team_agg_rows = (await session.execute(
        select(Team.league_id, func.count().label("team_count"))
        .group_by(Team.league_id)
    )).all()
    team_agg: dict[str, int] = {
        row.league_id: int(row.team_count or 0) for row in team_agg_rows
    }

    seasons_rows = (await session.execute(
        select(Event.league_id, Event.season_label)
        .where(
            Event.league_id.is_not(None),
            Event.season_label != "",
            Event.is_finalized.is_(True),
        )
        .distinct()
    )).all()
    seasons_by_league: dict[str, list[str]] = {}
    for row in seasons_rows:
        seasons_by_league.setdefault(row.league_id, []).append(row.season_label)
    for vals in seasons_by_league.values():
        vals.sort(reverse=True)

    out: list[dict] = []
    for lg in leagues:
        agg = event_agg.get(lg.id, {})
        out.append({
            "id": lg.id,
            "name": lg.name,
            "short_name": lg.short_name,
            "sport_id": lg.sport_id,
            "sport_name": lg.sport.name if lg.sport else None,
            "thesportsdb_id": lg.thesportsdb_id,
            "can_pull_historical_scores": lg.can_pull_historical_scores,
            "active": lg.active,
            "event_count": agg.get("event_count", 0),
            "team_count": team_agg.get(lg.id, 0),
            "last_event_at": agg.get("last_event_at"),
            "seasons_available": seasons_by_league.get(lg.id, []),
        })

    # Populated leagues first (most-events DESC), then alphabetical.
    out.sort(key=lambda r: (-r["event_count"], r["id"]))
    return {"leagues": out}


async def _league_or_404(session, league_id: str) -> League:
    league = await session.scalar(
        select(League).where(League.id == league_id)
    )
    if league is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "league not found")
    return league


async def _seasons_available(session, league_id: str) -> list[str]:
    rows = (await session.execute(
        select(Event.season_label)
        .where(
            Event.league_id == league_id,
            Event.season_label != "",
            Event.is_finalized.is_(True),
        )
        .distinct()
    )).all()
    return sorted({r.season_label for r in rows}, reverse=True)


@router.get("/leagues/{league_id}/fixtures")
async def league_fixtures(
    session: SessionDep,
    league_id: str,
    season: str | None = Query(default=None, description="season_label"),
    team_id: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict:
    """Fixtures (settled + scheduled) for one league, scoped to season.

    Settled rows include final scores; scheduled rows render with
    score=null. Sorted by start_time DESC (most recent first)."""
    league = await _league_or_404(session, league_id)

    seasons = await _seasons_available(session, league_id)
    season_label = season or (seasons[0] if seasons else "")

    stmt = (
        select(Event)
        .where(Event.league_id == league_id)
        .options(
            selectinload(Event.home_team),
            selectinload(Event.away_team),
        )
        .order_by(Event.start_time.desc(), Event.id)
        .limit(limit)
    )
    if season_label:
        stmt = stmt.where(Event.season_label == season_label)
    if team_id:
        stmt = stmt.where(
            or_(Event.home_team_id == team_id, Event.away_team_id == team_id)
        )

    rows = list(await session.scalars(stmt))

    fixtures = [_fixture_dto(ev) for ev in rows]
    return {
        "league_id": league.id,
        "league_name": league.name,
        "season_label": season_label,
        "seasons_available": seasons,
        "fixtures": fixtures,
        "pagination": {"cursor": None, "has_more": False},
    }


def _fixture_dto(ev: Event) -> dict:
    return {
        "event_id": ev.id,
        "start_time": ev.start_time,
        "season_label": ev.season_label,
        "status_type": ev.status_type,
        "status_display": ev.status_display,
        "home_team": _team_dto(ev.home_team),
        "away_team": _team_dto(ev.away_team),
        "home_score": ev.home_score,
        "away_score": ev.away_score,
        "winner_code": ev.winner_code,
        "is_finalized": ev.is_finalized,
        "provider": ev.provider,
    }


@router.get("/leagues/{league_id}/standings")
async def league_standings(
    session: SessionDep,
    league_id: str,
    season: str | None = Query(default=None, description="season_label"),
) -> dict:
    """Season standings computed from settled events. Soccer scoring
    (W=3, D=1, L=0) — sport-conditional dispatch lives in open question 4
    of the dashboard plan."""
    league = await _league_or_404(session, league_id)

    seasons = await _seasons_available(session, league_id)
    season_label = season or (seasons[0] if seasons else "")
    if not season_label:
        return {
            "league_id": league.id,
            "league_name": league.name,
            "season_label": "",
            "seasons_available": seasons,
            "standings": [],
            "computed_from_events": 0,
            "expected_events": 0,
        }

    events = list(await session.scalars(
        select(Event)
        .where(
            Event.league_id == league_id,
            Event.season_label == season_label,
            Event.is_finalized.is_(True),
        )
        .options(
            selectinload(Event.home_team),
            selectinload(Event.away_team),
        )
    ))

    expected = await session.scalar(
        select(func.count())
        .select_from(Event)
        .where(
            Event.league_id == league_id,
            Event.season_label == season_label,
        )
    ) or 0

    standings = _compute_standings(events)
    return {
        "league_id": league.id,
        "league_name": league.name,
        "season_label": season_label,
        "seasons_available": seasons,
        "standings": standings,
        "computed_from_events": len(events),
        "expected_events": int(expected),
    }


def _compute_standings(events: list[Event]) -> list[dict]:
    """Aggregate W/D/L, GF/GA, points per team. Teams with zero settled
    rows in the season don't appear — by design the table only reflects
    teams that have actually played."""
    by_team: dict[str, dict] = {}

    def _bucket(team_id: str, team_name: str) -> dict:
        b = by_team.get(team_id)
        if b is None:
            b = {
                "team_id": team_id,
                "team_name": team_name,
                "played": 0, "wins": 0, "draws": 0, "losses": 0,
                "goals_for": 0, "goals_against": 0,
            }
            by_team[team_id] = b
        return b

    for ev in events:
        if ev.home_team_id is None or ev.away_team_id is None:
            continue
        home_name = ev.home_team.name_long if ev.home_team else ev.home_team_id
        away_name = ev.away_team.name_long if ev.away_team else ev.away_team_id
        home = _bucket(ev.home_team_id, home_name)
        away = _bucket(ev.away_team_id, away_name)

        home_score = ev.home_score if ev.home_score is not None else 0
        away_score = ev.away_score if ev.away_score is not None else 0
        home["played"] += 1
        away["played"] += 1
        home["goals_for"] += home_score
        home["goals_against"] += away_score
        away["goals_for"] += away_score
        away["goals_against"] += home_score

        wc = ev.winner_code
        if wc == 1:
            home["wins"] += 1
            away["losses"] += 1
        elif wc == 2:
            away["wins"] += 1
            home["losses"] += 1
        elif wc == 3:
            home["draws"] += 1
            away["draws"] += 1
        # winner_code None on a finalized event = data quality issue; skip
        # silently so the row still counts toward played/GF/GA.

    rows = []
    for b in by_team.values():
        gd = b["goals_for"] - b["goals_against"]
        pts = b["wins"] * _POINTS_WIN + b["draws"] * _POINTS_DRAW
        rows.append({**b, "goal_difference": gd, "points": pts})
    rows.sort(
        key=lambda r: (-r["points"], -r["goal_difference"], -r["goals_for"], r["team_name"])
    )
    return rows


# --- Team detail -----------------------------------------------------------


@router.get("/teams/{team_id}/summary")
async def team_summary(
    session: SessionDep,
    team_id: str,
    season: str | None = Query(default=None, description="season_label"),
) -> dict:
    """Per-team form, season stats, home/away split, recent H2H opponents."""
    team = await session.scalar(
        select(Team).where(Team.id == team_id)
    )
    if team is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "team not found")

    league = await session.scalar(
        select(League).where(League.id == team.league_id)
    )
    league_name = league.name if league else team.league_id

    seasons = await _seasons_available(session, team.league_id)
    season_label = season or (seasons[0] if seasons else "")

    base = select(Event).where(
        or_(Event.home_team_id == team_id, Event.away_team_id == team_id),
        Event.is_finalized.is_(True),
    ).options(
        selectinload(Event.home_team),
        selectinload(Event.away_team),
    ).order_by(Event.start_time.desc(), Event.id)

    if season_label:
        base = base.where(Event.season_label == season_label)

    events = list(await session.scalars(base))

    season_stats = _team_season_stats(team_id, events)
    home_away_split = _team_home_away_split(team_id, events)
    form_last_10 = [_team_form_row(team_id, ev) for ev in events[:10]]
    h2h_recent = _team_h2h_recent(team_id, events, max_opponents=5)

    rank = await _team_rank_in_league(
        session, team.league_id, season_label, team_id
    ) if season_label else None
    if rank is not None and season_stats:
        season_stats["rank"] = rank

    return {
        "team": {
            "id": team.id,
            "team_id": team.team_id,
            "league_id": team.league_id,
            "league_name": league_name,
            "canonical_name": team.canonical_name,
            "name_long": team.name_long,
            "name_medium": team.name_medium,
            "name_short": team.name_short,
            "primary_color": team.primary_color,
            "secondary_color": team.secondary_color,
            "logo_url": team.logo_url,
        },
        "season_label": season_label,
        "seasons_available": seasons,
        "season_stats": season_stats,
        "home_away_split": home_away_split,
        "form_last_10": form_last_10,
        "h2h_recent": h2h_recent,
        # Elo lands in Phase B; v1 returns null and the template hides the card.
        "elo": None,
    }


def _team_result_letter(team_id: str, ev: Event) -> str:
    """Convert winner_code to W/D/L from this team's perspective."""
    wc = ev.winner_code
    if wc == 3:
        return "D"
    if wc == 1:  # home win
        return "W" if ev.home_team_id == team_id else "L"
    if wc == 2:  # away win
        return "W" if ev.away_team_id == team_id else "L"
    return "—"


def _team_season_stats(team_id: str, events: list[Event]) -> dict:
    if not events:
        return {}
    wins = draws = losses = gf = ga = 0
    for ev in events:
        is_home = ev.home_team_id == team_id
        ts = ev.home_score if is_home else ev.away_score
        os_ = ev.away_score if is_home else ev.home_score
        gf += ts or 0
        ga += os_ or 0
        letter = _team_result_letter(team_id, ev)
        if letter == "W":
            wins += 1
        elif letter == "D":
            draws += 1
        elif letter == "L":
            losses += 1
    return {
        "played": len(events),
        "wins": wins, "draws": draws, "losses": losses,
        "goals_for": gf, "goals_against": ga,
        "goal_difference": gf - ga,
        "points": wins * _POINTS_WIN + draws * _POINTS_DRAW,
    }


def _team_home_away_split(team_id: str, events: list[Event]) -> dict:
    def _init() -> dict:
        return {"played": 0, "wins": 0, "draws": 0, "losses": 0,
                "goals_for": 0, "goals_against": 0}
    home, away = _init(), _init()
    for ev in events:
        is_home = ev.home_team_id == team_id
        bucket = home if is_home else away
        ts = ev.home_score if is_home else ev.away_score
        os_ = ev.away_score if is_home else ev.home_score
        bucket["played"] += 1
        bucket["goals_for"] += ts or 0
        bucket["goals_against"] += os_ or 0
        letter = _team_result_letter(team_id, ev)
        if letter == "W":
            bucket["wins"] += 1
        elif letter == "D":
            bucket["draws"] += 1
        elif letter == "L":
            bucket["losses"] += 1
    return {"home": home, "away": away}


def _team_form_row(team_id: str, ev: Event) -> dict:
    is_home = ev.home_team_id == team_id
    opponent = ev.away_team if is_home else ev.home_team
    ts = ev.home_score if is_home else ev.away_score
    os_ = ev.away_score if is_home else ev.home_score
    return {
        "event_id": ev.id,
        "start_time": ev.start_time,
        "venue": "home" if is_home else "away",
        "opponent": (opponent.name_long if opponent else
                     (ev.away_team_id if is_home else ev.home_team_id)),
        "opponent_id": (opponent.id if opponent else
                        (ev.away_team_id if is_home else ev.home_team_id)),
        "result": _team_result_letter(team_id, ev),
        "team_score": ts,
        "opponent_score": os_,
        "score": f"{ts if ts is not None else '?'}-{os_ if os_ is not None else '?'}",
    }


def _team_h2h_recent(
    team_id: str, events: list[Event], *, max_opponents: int,
) -> list[dict]:
    by_opp: dict[str, dict] = {}
    order: list[str] = []
    for ev in events:
        is_home = ev.home_team_id == team_id
        opp_id = ev.away_team_id if is_home else ev.home_team_id
        opp_team = ev.away_team if is_home else ev.home_team
        if not opp_id:
            continue
        if opp_id not in by_opp:
            by_opp[opp_id] = {
                "opponent_id": opp_id,
                "opponent_name": (opp_team.name_long if opp_team else opp_id),
                "events": [],
            }
            order.append(opp_id)
        by_opp[opp_id]["events"].append(_team_form_row(team_id, ev))
    out: list[dict] = []
    for opp_id in order[:max_opponents]:
        block = by_opp[opp_id]
        block["last_5"] = [r["result"] for r in block["events"][:5]]
        out.append(block)
    return out


async def _team_rank_in_league(
    session, league_id: str, season_label: str, team_id: str,
) -> int | None:
    events = list(await session.scalars(
        select(Event)
        .where(
            Event.league_id == league_id,
            Event.season_label == season_label,
            Event.is_finalized.is_(True),
        )
        .options(
            selectinload(Event.home_team),
            selectinload(Event.away_team),
        )
    ))
    if not events:
        return None
    table = _compute_standings(events)
    for idx, row in enumerate(table, start=1):
        if row["team_id"] == team_id:
            return idx
    return None
