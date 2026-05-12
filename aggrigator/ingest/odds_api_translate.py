"""odds-api.io payload → SGO-shape payload translator.

The aggregator's ingest pipeline parses SGO-shape JSON via
``event_spec_from_payload`` in ``normalize.py``. Rather than rebuild the
pipeline for a second provider, we translate odds-api.io's response shape
INTO the SGO shape so normalize.py works unchanged.

Per the migration plan (aggrigator-plan/odds-api/odds-api.md §Scope):

- Only ``ML`` / ``Asian Handicap`` (main line) / ``Totals`` (main line) are
  translated. Everything else returned by ``/odds`` is dropped at the
  translator boundary.
- ``live`` events: dropped (yield no payload at all).
- ``settled`` events: translated to a SGO-shape event WITHOUT odds (event
  scores carry settlement; no per-bookmaker price snapshot needed).
- ``pending`` events: translated WITH odds when ``/odds`` data is provided.

Per aggrigator-plan/odds-api/cancelled-suspended.md §4:

- ``settled`` with null scores → VOID-shape payload (status.cancelled=True).
- ``pending`` with empty bookmakers → event-only payload (no odds dict).
  Mirrors the WebSocket protocol's ``no_markets`` event semantics.

The slug maps (sport + league) live here because translation is what cares
about them. They start hardcoded; Phase 0 probe
(``scripts/probe_oddsapi.py``) confirms the values empirically.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from aggrigator.ingest.converters import decimal_to_american
from aggrigator.ingest.odds_api_errors import SchemaError

logger = logging.getLogger(__name__)


# ---- slug maps (odds-api.io ↔ SGO) -----------------------------------------
# Hardcoded defaults — Phase 0 probe (scripts/probe_oddsapi.py) confirms or
# corrects these. Unknown leagues are logged + skipped, so a missing entry is
# observable but not crashing. Extend as new leagues come online.

SGO_TO_ODDSAPI_SPORT: dict[str, str] = {
    "BASEBALL": "baseball",
    "BASKETBALL": "basketball",
    "FOOTBALL": "american-football",
    "HOCKEY": "ice-hockey",
    "SOCCER": "football",
    "TENNIS": "tennis",
}
ODDSAPI_TO_SGO_SPORT: dict[str, str] = {v: k for k, v in SGO_TO_ODDSAPI_SPORT.items()}

# League slug map. Conservative seed — verify against /leagues?sport= during
# Phase 0 and update entries that don't match.
SGO_TO_ODDSAPI_LEAGUE: dict[str, str] = {
    "MLB": "usa-mlb",
    "MLS": "usa-mls",
    "NBA": "usa-nba",
    "NFL": "usa-nfl",
    "NHL": "usa-nhl",
    "UEFA_CHAMPIONS_LEAGUE": "europe-uefa-champions-league",
    "EPL": "england-premier-league",
}
ODDSAPI_TO_SGO_LEAGUE: dict[str, str] = {v: k for k, v in SGO_TO_ODDSAPI_LEAGUE.items()}


# ---- statID per sport (what SGO oddIDs encode) -----------------------------
# SGO oddID format: ``{statID}-{statEntityID}-{periodID}-{betTypeID}-{sideID}``.
# The statID changes by sport. We pick the canonical scoring stat per sport so
# the synthesized oddIDs hit the right BET_TYPE_TO_CATEGORY entries in
# taxonomy.py.

_SGO_SPORT_TO_STAT_ID: dict[str, str] = {
    "BASEBALL": "runs",
    "BASKETBALL": "points",
    "FOOTBALL": "points",  # American football
    "HOCKEY": "goals",
    "SOCCER": "goals",
    "TENNIS": "points",
}


# ---- entry points ----------------------------------------------------------


def to_sgo_event_payload(
    event_dict: dict[str, Any],
    odds_dict: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Translate ONE odds-api.io event (+ optional odds payload) to SGO shape.

    Returns ``None`` when the event should be dropped (e.g. status=live in
    pre-match-only scope, or unknown sport slug). Raises ``SchemaError`` if
    a required field is missing — caller logs + skips per event.

    Three branches based on status:

    - ``live`` → ``None`` (caller skips).
    - ``settled`` → event-only SGO payload with finalized=True. If scores are
      null, returns a VOID-shape payload (status.cancelled=True) so the
      lifecycle path treats it as cancelled rather than finalized.
    - ``pending`` → event + odds. If ``odds_dict`` is missing or has empty
      ``bookmakers``, returns event-only (mirrors WebSocket ``no_markets``).
    """
    _require(event_dict, "id")
    _require(event_dict, "status")
    _require(event_dict, "home")
    _require(event_dict, "away")
    _require(event_dict, "date")

    status = event_dict["status"]
    if status == "live":
        return None  # out of scope this round

    sport_slug = (event_dict.get("sport") or {}).get("slug") or ""
    league_slug = (event_dict.get("league") or {}).get("slug") or ""
    sport_id = ODDSAPI_TO_SGO_SPORT.get(sport_slug)
    league_id = ODDSAPI_TO_SGO_LEAGUE.get(league_slug)
    if sport_id is None:
        logger.warning(
            "odds-api translate: unknown sport slug %r — skipping event %s",
            sport_slug, event_dict.get("id"),
        )
        return None
    if league_id is None:
        logger.warning(
            "odds-api translate: unknown league slug %r — skipping event %s",
            league_slug, event_dict.get("id"),
        )
        return None

    scores = event_dict.get("scores") or {}

    if status == "settled":
        # Guard per cancelled-suspended.md §4.1: a "settled" event with null
        # scores is an abandoned game or feed error, not a real finalize.
        # Translate as cancelled so the downstream lifecycle treats it as
        # VOID, not FINALIZED.
        if scores.get("home") is None or scores.get("away") is None:
            return _void_event_payload(event_dict, sport_id, league_id)
        return _settled_event_payload(event_dict, sport_id, league_id)

    # status == "pending"
    if odds_dict is None or not (odds_dict.get("bookmakers") or {}):
        # WebSocket "no_markets" REST equivalent: event exists but no books
        # are offering it right now. Yield the event without odds so the
        # row stays in our listing and any prior quotes are preserved by
        # the upsert path.
        return _pending_event_payload(event_dict, sport_id, league_id, odds={})

    odds = _translate_odds(odds_dict, sport_id)
    return _pending_event_payload(event_dict, sport_id, league_id, odds=odds)


# ---- payload builders ------------------------------------------------------


def _base_payload(
    event_dict: dict[str, Any],
    sport_id: str,
    league_id: str,
) -> dict[str, Any]:
    """Common SGO-shape skeleton — teams, IDs, type=match."""
    return {
        "type": "match",
        "eventID": str(event_dict["id"]),
        "sportID": sport_id,
        "leagueID": league_id,
        "info": {"seasonWeek": ""},
        "teams": {
            "home": _team_dict(event_dict, "home"),
            "away": _team_dict(event_dict, "away"),
        },
    }


def _team_dict(event_dict: dict[str, Any], side: str) -> dict[str, Any]:
    """``side`` is ``"home"`` or ``"away"``. odds-api.io ships flat
    ``home``/``away`` names and ``homeId``/``awayId`` numeric ids."""
    name = str(event_dict.get(side) or "")
    raw_team_id = event_dict.get(f"{side}Id")
    team_id = str(raw_team_id) if raw_team_id is not None else ""
    scores = event_dict.get("scores") or {}
    raw_score = scores.get(side)
    score: int | None = None
    if isinstance(raw_score, (int, float)):
        score = int(raw_score)
    return {
        "teamID": team_id,
        "names": {"long": name, "medium": name[:64], "short": name[:32]},
        "score": score,
    }


def _pending_event_payload(
    event_dict: dict[str, Any],
    sport_id: str,
    league_id: str,
    *,
    odds: dict[str, Any],
) -> dict[str, Any]:
    payload = _base_payload(event_dict, sport_id, league_id)
    payload["status"] = {
        "startsAt": event_dict["date"],
        "started": False,
        "ended": False,
        "completed": False,
        "finalized": False,
        "live": False,
        "cancelled": False,
        "delayed": False,
        "displayLong": "Pending",
        "displayShort": "Pending",
        "currentPeriodID": "",
    }
    payload["odds"] = odds
    return payload


def _settled_event_payload(
    event_dict: dict[str, Any],
    sport_id: str,
    league_id: str,
) -> dict[str, Any]:
    payload = _base_payload(event_dict, sport_id, league_id)
    payload["status"] = {
        "startsAt": event_dict["date"],
        "started": True,
        "ended": True,
        "completed": True,
        "finalized": True,
        "live": False,
        "cancelled": False,
        "delayed": False,
        "displayLong": "Final",
        "displayShort": "Final",
        "currentPeriodID": "",
    }
    payload["odds"] = {}
    return payload


def _void_event_payload(
    event_dict: dict[str, Any],
    sport_id: str,
    league_id: str,
) -> dict[str, Any]:
    payload = _base_payload(event_dict, sport_id, league_id)
    payload["status"] = {
        "startsAt": event_dict["date"],
        "started": False,
        "ended": True,
        "completed": False,
        "finalized": False,
        "live": False,
        "cancelled": True,  # drives _derive_status_type → "canceled"
        "delayed": False,
        "displayLong": "Cancelled",
        "displayShort": "Cancelled",
        "currentPeriodID": "",
    }
    payload["odds"] = {}
    return payload


# ---- odds translation ------------------------------------------------------

# Bookmaker name normalization. odds-api.io ships display names ("DraftKings");
# our internal bookmaker_id convention is lowercase. Mirrors what
# write_markets / Bookmaker.id expect. Keep aligned with
# odds_api_http.ODDSAPI_BOOKMAKERS — names not in the map fall through to
# str.lower() which is good enough for the catalogued books we send.
_BOOKMAKER_DISPLAY_TO_ID: dict[str, str] = {
    "DraftKings": "draftkings",
    "FanDuel": "fanduel",
    # Legacy entries retained so historical fixtures still translate.
    "Bet365": "bet365",
    "Pinnacle": "pinnacle",
}


def _normalize_book_id(display_name: str) -> str:
    return _BOOKMAKER_DISPLAY_TO_ID.get(display_name, display_name.lower())


def _translate_odds(
    odds_dict: dict[str, Any], sport_id: str,
) -> dict[str, dict[str, Any]]:
    """Flatten odds-api.io's per-bookmaker market list into SGO's
    oddID-keyed dict. Only ML / AH-main / Totals-main rows survive — the rest
    are dropped at this boundary.
    """
    stat_id = _SGO_SPORT_TO_STAT_ID.get(sport_id, "points")
    bookmakers = odds_dict.get("bookmakers") or {}
    urls = odds_dict.get("urls") or {}

    # accumulator: { oddID: { "oddID":..., "byBookmaker": { book: {...} } } }
    odds_by_id: dict[str, dict[str, Any]] = {}

    for book_display_name, markets in bookmakers.items():
        if not isinstance(markets, list):
            continue
        book_id = _normalize_book_id(book_display_name)
        deeplink = urls.get(book_display_name) or ""

        for market in markets:
            if not isinstance(market, dict):
                continue
            mname = market.get("name") or ""
            updated_at = market.get("updatedAt") or ""
            entries = market.get("odds") or []
            if not isinstance(entries, list) or not entries:
                continue

            if mname == "ML":
                # 3-way for soccer (draw present), 2-way otherwise.
                row = entries[0]
                _accumulate_ml(
                    odds_by_id, row, stat_id, sport_id, book_id,
                    deeplink, updated_at,
                )
            elif mname == "Asian Handicap":
                row = _pick_main_line_handicap(entries)
                if row is None:
                    continue
                _accumulate_handicap(
                    odds_by_id, row, stat_id, book_id, deeplink, updated_at,
                )
            elif mname == "Totals":
                row = _pick_main_line_totals(entries, sport_id)
                if row is None:
                    continue
                _accumulate_totals(
                    odds_by_id, row, stat_id, book_id, deeplink, updated_at,
                )
            # else: silently drop (player props, BTTS, period markets, ...)

    if not odds_by_id and bookmakers:
        # Loud signal for the next time the bookmaker name catalog drifts:
        # we got bookmakers in the response but produced zero translated
        # rows. Either the books returned an unexpected shape or the
        # market-name filter dropped everything.
        logger.warning(
            "odds-api translate: bookmakers=%s returned 0 translatable "
            "odd rows — check market shapes in odds_api_translate._translate_odds",
            list(bookmakers.keys()),
        )
    return odds_by_id


def _pick_main_line_handicap(entries: list[dict]) -> dict | None:
    """Asian Handicap row with the smallest |hdp| — the "pick" / closest-to-pk
    line. odds-api.io returns the alt-line ladder; we want one row.
    """
    def _abs_hdp(row: dict) -> float:
        try:
            return abs(float(row.get("hdp", 0) or 0))
        except (TypeError, ValueError):
            return float("inf")
    return min(entries, key=_abs_hdp, default=None)


# Canonical Totals line per sport (cancellable by support during Phase 0).
# Used as the tiebreaker when picking which alt-line row is "main."
_CANONICAL_TOTAL: dict[str, Decimal] = {
    "BASEBALL": Decimal("8.5"),
    "BASKETBALL": Decimal("220.5"),
    "FOOTBALL": Decimal("45.5"),  # American football
    "HOCKEY": Decimal("5.5"),
    "SOCCER": Decimal("2.5"),
    "TENNIS": Decimal("22.5"),
}


def _pick_main_line_totals(entries: list[dict], sport_id: str) -> dict | None:
    """Totals row closest to the sport's canonical total line, or median if
    no canonical is configured."""
    canonical = _CANONICAL_TOTAL.get(sport_id)
    if canonical is None:
        # Fallback: pick the median of the ladder.
        try:
            sorted_rows = sorted(entries, key=lambda r: float(r.get("hdp", 0) or 0))
        except (TypeError, ValueError):
            return entries[0] if entries else None
        return sorted_rows[len(sorted_rows) // 2] if sorted_rows else None

    def _dist(row: dict) -> Decimal:
        try:
            return abs(Decimal(str(row.get("hdp", 0) or 0)) - canonical)
        except (InvalidOperation, TypeError):
            return Decimal("999999")
    return min(entries, key=_dist, default=None)


def _accumulate_ml(
    odds_by_id: dict[str, dict[str, Any]],
    row: dict,
    stat_id: str,
    sport_id: str,
    book_id: str,
    deeplink: str,
    updated_at: str,
) -> None:
    """ML row has ``home``/``away`` (always) and ``draw`` (soccer only)."""
    has_draw = row.get("draw") not in (None, "")
    bet_type = "ml3way" if has_draw else "ml"

    for side_label, side_id, side_stat_entity in (
        ("home", "home", "home"),
        ("away", "away", "away"),
    ):
        price = row.get(side_label)
        if price in (None, ""):
            continue
        odd_id = f"{stat_id}-{side_stat_entity}-game-{bet_type}-{side_id}"
        _add_book_quote(
            odds_by_id, odd_id, price, book_id, deeplink, updated_at,
        )

    if has_draw:
        odd_id = f"{stat_id}-draw-game-ml3way-draw"
        _add_book_quote(
            odds_by_id, odd_id, row["draw"], book_id, deeplink, updated_at,
        )


def _accumulate_handicap(
    odds_by_id: dict[str, dict[str, Any]],
    row: dict,
    stat_id: str,
    book_id: str,
    deeplink: str,
    updated_at: str,
) -> None:
    """Asian Handicap: home gets ``hdp``, away gets ``-hdp``."""
    try:
        hdp = Decimal(str(row.get("hdp", 0) or 0))
    except (InvalidOperation, TypeError):
        return

    home_price = row.get("home")
    away_price = row.get("away")
    if home_price not in (None, ""):
        odd_id = f"{stat_id}-home-game-sp-home"
        _add_book_quote(
            odds_by_id, odd_id, home_price, book_id, deeplink, updated_at,
            spread=hdp,
        )
        # SGO's normalize.py reads fairSpread/bookSpread for the canonical
        # line. Set both so normalize can pick either.
        _set_market_line(odds_by_id, odd_id, "fairSpread", hdp)
    if away_price not in (None, ""):
        odd_id = f"{stat_id}-away-game-sp-away"
        _add_book_quote(
            odds_by_id, odd_id, away_price, book_id, deeplink, updated_at,
            spread=-hdp,
        )
        _set_market_line(odds_by_id, odd_id, "fairSpread", -hdp)


def _accumulate_totals(
    odds_by_id: dict[str, dict[str, Any]],
    row: dict,
    stat_id: str,
    book_id: str,
    deeplink: str,
    updated_at: str,
) -> None:
    try:
        line = Decimal(str(row.get("hdp", 0) or 0))
    except (InvalidOperation, TypeError):
        return

    for side_label, side_id in (("over", "over"), ("under", "under")):
        price = row.get(side_label)
        if price in (None, ""):
            continue
        odd_id = f"{stat_id}-all-game-ou-{side_id}"
        _add_book_quote(
            odds_by_id, odd_id, price, book_id, deeplink, updated_at,
            over_under=line,
        )
        _set_market_line(odds_by_id, odd_id, "fairOverUnder", line)


# ---- odds-row helpers ------------------------------------------------------


def _add_book_quote(
    odds_by_id: dict[str, dict[str, Any]],
    odd_id: str,
    decimal_price: Any,
    book_id: str,
    deeplink: str,
    updated_at: str,
    *,
    spread: Decimal | None = None,
    over_under: Decimal | None = None,
) -> None:
    """Insert/merge one per-bookmaker quote into the SGO-shape odds dict."""
    decimal_odds = _parse_decimal_odds(decimal_price)
    if decimal_odds is None:
        return
    american = decimal_to_american(decimal_odds)
    if american == 0:
        return

    row = odds_by_id.setdefault(odd_id, {
        "oddID": odd_id,
        "fairOdds": None,
        "bookOdds": None,
        "byBookmaker": {},
        "bookOddsAvailable": True,
    })
    # First book sets the top-level fair/book price (used as fallback in
    # normalize.py when byBookmaker is empty). Later books don't overwrite —
    # we want a stable "fair" reference, and any per-book differences are
    # captured in the byBookmaker map.
    if row["fairOdds"] is None:
        row["fairOdds"] = _format_american(american)
        row["bookOdds"] = _format_american(american)
    row["byBookmaker"][book_id] = {
        "odds": _format_american(american),
        "available": True,
        "deeplink": deeplink,
        "lastUpdatedAt": updated_at or _now_iso(),
        **({"spread": str(spread)} if spread is not None else {}),
        **({"overUnder": str(over_under)} if over_under is not None else {}),
    }


def _set_market_line(
    odds_by_id: dict[str, dict[str, Any]],
    odd_id: str,
    key: str,
    value: Decimal,
) -> None:
    """Set the canonical line key (``fairSpread`` / ``fairOverUnder``) on
    the SGO-shape odd dict. Idempotent — only writes if not already set."""
    row = odds_by_id.get(odd_id)
    if row is None:
        return
    if key not in row:
        row[key] = str(value)


def _parse_decimal_odds(value: Any) -> Decimal | None:
    """odds-api.io decimal odds arrive as strings like ``"2.10"``. Parse to
    Decimal, return ``None`` for unparseable / out-of-range values."""
    if value in (None, "", 0, "0"):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None
    if d <= Decimal("1.0"):
        return None
    return d


def _format_american(american: int) -> str:
    """``+150`` / ``-130`` SGO-compatible string format."""
    return f"+{american}" if american > 0 else str(american)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _require(payload: dict, field: str) -> None:
    if field not in payload or payload[field] in (None, ""):
        raise SchemaError(
            f"odds-api translate: missing required field {field!r} "
            f"on event payload {payload.get('id', '?')}"
        )
