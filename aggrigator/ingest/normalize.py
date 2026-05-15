"""Provider payload normalization — verbatim port of MDProject's


Pure functions: provider event payload -> ``EventSpec`` / ``MarketSpec`` /
``SelectionSpec`` / ``BookmakerQuoteSpec``. No DB, no Django, no I/O.

The parity test (``tests/parity/test_normalize_parity.py``) imports BOTH this
module and MDProject's, runs the same fixture through each, and asserts the
specs are equal. Keep this file mechanically aligned with MDProject's — when
their normalizer changes, port the change here and re-run parity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from aggrigator.ingest.converters import american_to_decimal
from aggrigator.ingest.taxonomy import (
    BET_TYPE_TO_CATEGORY,
    PERIOD_TO_SCOPE,
    SIDE_TO_SELECTION,
    OddIDParts,
    market_type_for,
    parse_odd_id,
)

logger = logging.getLogger(__name__)


# ---- spec dataclasses -------------------------------------------------------


@dataclass
class TeamSpec:
    team_id: str
    league_id: str
    name_long: str
    name_medium: str
    name_short: str
    primary_color: str | None = None
    secondary_color: str | None = None
    primary_contrast: str | None = None
    secondary_contrast: str | None = None
    stat_entity_id: str = ""
    score: int | None = None

    @property
    def pk(self) -> str:
        return f"{self.league_id}:{self.team_id}"


@dataclass
class BookmakerQuoteSpec:
    bookmaker_id: str
    decimal_odds: Decimal | None
    spread: Decimal | None
    over_under: Decimal | None
    available: bool
    deeplink: str
    last_updated_at: datetime | None


@dataclass
class SelectionSpec:
    selection_id: str
    market_id: str
    selection_type: str
    label: str
    decimal_odds: Decimal | None
    opening_decimal_odds: Decimal | None
    suspended: bool
    raw_side_id: str
    raw_stat_entity_id: str
    score: float | None
    by_bookmaker: list[BookmakerQuoteSpec] = field(default_factory=list)


@dataclass
class MarketSpec:
    market_id: str
    event_id: str
    league_id: str
    sport_id: str
    category: str
    market_type: str
    scope: str
    line: Decimal | None
    side: str
    subject_team_pk: str | None
    provider_market_id: str
    is_live: bool
    suspended: bool
    last_updated_at: datetime
    selections: list[SelectionSpec] = field(default_factory=list)


@dataclass
class EventSpec:
    event_id: str
    sport_id: str
    league_id: str
    type: str
    season_label: str
    start_time: datetime
    status_type: str
    status_display: str
    current_period_id: str
    is_live: bool
    is_finalized: bool
    completed: bool
    home_team: TeamSpec
    away_team: TeamSpec
    home_score: int | None
    away_score: int | None
    winner_code: int | None
    feed_locked: bool
    markets: list[MarketSpec] = field(default_factory=list)


# ---- top-level entry --------------------------------------------------------


def event_spec_from_payload(payload: dict) -> EventSpec | None:
    """Convert one provider event payload into an ``EventSpec``.

    Returns ``None`` for non-match event types since they have no teams.
    """
    if payload.get("type") != "match":
        return None
    teams = payload.get("teams") or {}
    if not teams.get("home") or not teams.get("away"):
        return None

    league_id = payload.get("leagueID", "")
    sport_id = payload.get("sportID", "")
    event_id = payload.get("eventID", "")
    if not (league_id and sport_id and event_id):
        return None

    status = payload.get("status") or {}
    home = _team_spec(teams["home"], league_id)
    away = _team_spec(teams["away"], league_id)

    home_score = home.score
    away_score = away.score
    winner_code: int | None = None
    if status.get("finalized") and home_score is not None and away_score is not None:
        if home_score > away_score:
            winner_code = 1
        elif away_score > home_score:
            winner_code = 2
        else:
            winner_code = 3

    return EventSpec(
        event_id=event_id,
        sport_id=sport_id,
        league_id=league_id,
        type=payload["type"],
        season_label=(payload.get("info") or {}).get("seasonWeek", "") or "",
        start_time=_parse_iso(status.get("startsAt"))
        or datetime.fromtimestamp(0, tz=timezone.utc),
        status_type=_derive_status_type(status),
        status_display=status.get("displayLong") or status.get("displayShort") or "",
        current_period_id=status.get("currentPeriodID") or "",
        is_live=bool(status.get("live")),
        is_finalized=bool(status.get("finalized")),
        completed=bool(status.get("completed")),
        home_team=home,
        away_team=away,
        home_score=home_score,
        away_score=away_score,
        winner_code=winner_code,
        feed_locked=bool(status.get("cancelled")),
        markets=_market_specs(payload, sport_id, league_id, event_id, status),
    )


# ---- helpers ----------------------------------------------------------------


def _derive_status_type(status: dict) -> str:
    if status.get("cancelled"):
        return "canceled"
    if status.get("finalized") and status.get("ended"):
        return "finished"
    if status.get("live"):
        return "inprogress"
    if status.get("delayed"):
        return "postponed"
    return "notstarted"


def _team_spec(team_payload: dict, league_id: str) -> TeamSpec:
    names = team_payload.get("names") or {}
    colors = team_payload.get("colors") or {}
    score = team_payload.get("score")
    return TeamSpec(
        team_id=team_payload.get("teamID", ""),
        league_id=league_id,
        name_long=(names.get("long") or "")[:128],
        name_medium=(names.get("medium") or "")[:64],
        name_short=(names.get("short") or "")[:32],
        primary_color=_hex(colors.get("primary")),
        secondary_color=_hex(colors.get("secondary")),
        primary_contrast=_hex(colors.get("primaryContrast")),
        secondary_contrast=_hex(colors.get("secondaryContrast")),
        stat_entity_id=team_payload.get("statEntityID") or "",
        score=int(score) if isinstance(score, (int, float)) else None,
    )


def _hex(value) -> str | None:
    if not value:
        return None
    s = str(value).strip()
    return s[:9] if s else None


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    s = str(value).rstrip("Z")
    try:
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _market_specs(
    payload: dict, sport_id: str, league_id: str, event_id: str, status: dict
) -> list[MarketSpec]:
    odds = payload.get("odds") or {}
    if not odds:
        return []

    now = datetime.now(timezone.utc)
    is_live = bool(status.get("live"))

    groups: dict[tuple, list[tuple[OddIDParts, dict]]] = {}
    for odd_id, odd in odds.items():
        try:
            parts = parse_odd_id(odd_id)
        except ValueError:
            logger.debug("skipping malformed oddID %r", odd_id)
            continue
        if parts.betTypeID not in BET_TYPE_TO_CATEGORY:
            continue
        if parts.statEntityID not in ("home", "away", "all"):
            continue  # player prop — out of scope
        if parts.periodID not in PERIOD_TO_SCOPE:
            logger.debug("skipping unknown periodID=%s on %s", parts.periodID, odd_id)
            continue

        key = _group_key(parts, odd)
        if key is None:
            continue
        groups.setdefault(key, []).append((parts, odd))

    out: list[MarketSpec] = []
    for key, members in groups.items():
        spec = _build_market_spec(key, members, sport_id, league_id, event_id, is_live, now)
        if spec is not None:
            out.append(spec)
    return out


def _group_key(parts: OddIDParts, odd: dict) -> tuple | None:
    bt = parts.betTypeID
    if bt == "ml":
        return (parts.statID, parts.periodID, "ml")
    if bt == "ml3way":
        return (parts.statID, parts.periodID, "ml3way")
    if bt == "sp":
        return (parts.statID, parts.periodID, "sp")
    if bt == "ou":
        line_key = _line_string(odd)
        if parts.statEntityID == "all":
            return (parts.statID, parts.periodID, "ou", "all", line_key)
        return (parts.statID, parts.periodID, "ou", parts.statEntityID, line_key)
    if bt == "yn":
        return (parts.statID, parts.periodID, "yn", parts.statEntityID)
    if bt == "eo":
        return (parts.statID, parts.periodID, "eo", parts.statEntityID)
    return None


def _line_string(odd: dict) -> str:
    for key in ("fairOverUnder", "bookOverUnder", "openFairOverUnder", "openBookOverUnder"):
        v = odd.get(key)
        if v not in (None, ""):
            return str(v)
    return ""


def _build_market_spec(
    key: tuple,
    members: list[tuple[OddIDParts, dict]],
    sport_id: str,
    league_id: str,
    event_id: str,
    is_live: bool,
    now: datetime,
) -> MarketSpec | None:
    if not members:
        return None
    first_parts, first_odd = members[0]
    bt = first_parts.betTypeID
    category = BET_TYPE_TO_CATEGORY[bt]
    scope = PERIOD_TO_SCOPE[first_parts.periodID]

    side_marker = ""
    if bt in ("ou", "yn") and first_parts.statEntityID in ("home", "away"):
        scoring_stats = {"points", "goals", "runs"}
        if first_parts.statID in scoring_stats:
            category = "PROPS_TEAM"
            side_marker = first_parts.statEntityID.upper()

    line = _canonical_line(bt, members)

    market_type = market_type_for(first_parts, league_id)
    market_id = build_market_id(
        event_id=event_id, kind=bt, scope=scope, line=line, side=side_marker,
        stat_id=first_parts.statID, stat_entity_id=first_parts.statEntityID,
    )

    suspended = all(bool(o.get("cancelled")) for _, o in members)

    spec = MarketSpec(
        market_id=market_id,
        event_id=event_id,
        league_id=league_id,
        sport_id=sport_id,
        category=category,
        market_type=market_type,
        scope=scope,
        line=line,
        side=side_marker,
        subject_team_pk=None,
        provider_market_id=first_odd.get("oddID", ""),
        is_live=is_live,
        suspended=suspended,
        last_updated_at=now,
    )

    for parts, odd in members:
        spec.selections.append(_selection_spec(parts, odd, market_id))
    return spec


def _canonical_line(bet_type: str, members: list[tuple[OddIDParts, dict]]) -> Decimal | None:
    if bet_type == "sp":
        for parts, odd in members:
            if parts.sideID == "home":
                v = _line_for(parts, odd)
                if v is not None:
                    return v
        for parts, odd in members:
            if parts.sideID == "away":
                v = _line_for(parts, odd)
                if v is not None:
                    return -v
        return None
    if bet_type == "ou":
        for parts, odd in members:
            v = _line_for(parts, odd)
            if v is not None:
                return v
        return None
    return None


def _line_for(parts: OddIDParts, odd: dict) -> Decimal | None:
    if parts.betTypeID == "sp":
        for key in ("fairSpread", "bookSpread", "openFairSpread", "openBookSpread"):
            v = odd.get(key)
            if v not in (None, ""):
                try:
                    return Decimal(str(v))
                except Exception:  # noqa: BLE001
                    return None
        return None
    if parts.betTypeID == "ou":
        for key in ("fairOverUnder", "bookOverUnder", "openFairOverUnder", "openBookOverUnder"):
            v = odd.get(key)
            if v not in (None, ""):
                try:
                    return Decimal(str(v))
                except Exception:  # noqa: BLE001
                    return None
        return None
    return None


def _selection_spec(parts: OddIDParts, odd: dict, market_id: str) -> SelectionSpec:
    sel_type = SIDE_TO_SELECTION.get(parts.sideID, "CUSTOM")
    fair_dec = american_to_decimal(odd.get("fairOdds"))
    book_dec = american_to_decimal(odd.get("bookOdds"))
    decimal_odds = fair_dec if fair_dec is not None else book_dec

    open_fair = american_to_decimal(odd.get("openFairOdds"))
    open_book = american_to_decimal(odd.get("openBookOdds"))
    opening = open_fair if open_fair is not None else open_book

    score = odd.get("score")
    score_val: float | None
    if isinstance(score, (int, float)):
        score_val = float(score)
    else:
        score_val = None

    book_quotes = _book_quote_specs(odd.get("byBookmaker") or {})

    selection_id = f"{market_id}:{parts.statEntityID}-{parts.sideID}"
    label = (odd.get("marketName") or f"{parts.statEntityID} {parts.sideID}").strip()[:128]

    return SelectionSpec(
        selection_id=selection_id,
        market_id=market_id,
        selection_type=sel_type,
        label=label,
        decimal_odds=decimal_odds,
        opening_decimal_odds=opening,
        suspended=not bool(odd.get("bookOddsAvailable", True)),
        raw_side_id=parts.sideID,
        raw_stat_entity_id=parts.statEntityID,
        score=score_val,
        by_bookmaker=book_quotes,
    )


def _book_quote_specs(by_bookmaker: dict) -> list[BookmakerQuoteSpec]:
    out: list[BookmakerQuoteSpec] = []
    for book_id, q in by_bookmaker.items():
        if not isinstance(q, dict):
            continue
        out.append(BookmakerQuoteSpec(
            bookmaker_id=book_id,
            decimal_odds=american_to_decimal(q.get("odds")),
            spread=_dec(q.get("spread")),
            over_under=_dec(q.get("overUnder")),
            available=bool(q.get("available", True)),
            deeplink=str(q.get("deeplink") or "")[:500],
            last_updated_at=_parse_iso(q.get("lastUpdatedAt")),
        ))
    return out


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


# ---- deterministic market id ------------------------------------------------


_SCOPE_TO_SLUG = {
    "FULL_GAME": "ft",
    "H1": "h1", "H2": "h2",
    "Q1": "q1", "Q2": "q2", "Q3": "q3", "Q4": "q4",
    "P1": "p1", "P2": "p2", "P3": "p3",
    "OVERTIME": "ot",
    "SHOOTOUT": "so",
    "INNINGS_1_5": "i1_5",
    "INNINGS_1_3": "i1_3",
    "MINUTES_5": "m5",
    "MINUTES_10": "m10",
    "INNING_1": "i1", "INNING_2": "i2", "INNING_3": "i3",
    "INNING_4": "i4", "INNING_5": "i5", "INNING_6": "i6",
    "INNING_7": "i7", "INNING_8": "i8", "INNING_9": "i9",
}


def build_market_id(
    *, event_id: str, kind: str, scope: str,
    line: Decimal | None = None, side: str = "",
    stat_id: str = "", stat_entity_id: str = "",
) -> str:
    """Deterministic ``Market.id`` for an provider-sourced market.

    Includes ``statID`` and ``statEntityID`` whenever the group key uses them
    (per plan §7.7 #3) so distinct ``eo``/``yn`` groups don't collide on the
    same id.
    """
    scope_slug = _SCOPE_TO_SLUG.get(scope, scope.lower())
    parts = [event_id, kind, scope_slug]
    if stat_id:
        parts.append(stat_id.lower())
    if stat_entity_id:
        parts.append(stat_entity_id.lower())
    if line is not None:
        parts.append(_line_slug(line))
    if side:
        parts.append(side.lower())
    return "-".join(parts)


def _line_slug(line: Decimal) -> str:
    s = format(line, "f")
    if s.startswith("-"):
        return "neg" + s[1:].replace(".", "_")
    return s.replace(".", "_")


# ---- bulk entry -------------------------------------------------------------


def event_specs_from_response(response: dict | Iterable[dict]) -> list[EventSpec]:
    """Convert an provider ``GET /events`` response (or its ``data`` list, or a
    single event payload) into a list of EventSpec — non-match events filtered.
    """
    if isinstance(response, dict):
        if "data" in response and isinstance(response["data"], list):
            payloads = response["data"]
        else:
            payloads = [response]
    else:
        payloads = list(response)

    out: list[EventSpec] = []
    for p in payloads:
        spec = event_spec_from_payload(p)
        if spec is not None:
            out.append(spec)
    return out
