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
