"""Translate TheSportsDB ``eventsround.php`` payloads into ``EventSpec``.

Pure functions — no DB, no I/O. The historical orchestrator handles
team-row lookup (by ``Team.thesportsdb_team_id``) and feeds resolved
Team rows into the translator alongside the raw provider payload.

Payload shape (verified 2026-05-19 against EPL round 1 of 2024-2025):

  {
    "idEvent": "2069556",
    "idLeague": "4328",
    "strSeason": "2024-2025",
    "intRound": "1",
    "dateEvent": "2024-08-16",
    "strTime": "19:00:00",
    "strTimestamp": "2024-08-16T19:00:00",
    "idHomeTeam": "133612",
    "strHomeTeam": "Manchester United",
    "intHomeScore": "1",
    "idAwayTeam": "133600",
    "strAwayTeam": "Fulham",
    "intAwayScore": "0",
    "strStatus": "Match Finished",
    "strPostponed": "no",
  }

Internal target: ``EventSpec`` from ``aggrigator.ingest.normalize`` —
same dataclass the live (odds-api.io) path produces. ``EventSpec.markets``
is always the empty list — TheSportsDB has no odds, no markets are
written downstream.

Event ID namespace: TheSportsDB ids are integers like "2069556"; we
prefix to ``"tsdb:2069556"`` so the aggrigator's
``core_event_event.id`` (a VARCHAR(64)) can never collide with the
odds-api.io namespace (numeric strings without a prefix).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aggrigator.ingest.normalize import EventSpec, TeamSpec
from aggrigator.models import Team

logger = logging.getLogger(__name__)


# ---- helpers --------------------------------------------------------------


def _parse_score(value) -> int | None:
    """TheSportsDB serializes scores as string ints (e.g. ``"3"``) or
    ``null`` when the match hasn't finished. Postponed games sometimes
    carry empty strings — treat anything non-numeric as missing."""
    if value in (None, "", "null"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _winner_code(home: int | None, away: int | None) -> int | None:
    """1 = home win, 2 = away, 3 = draw. None if either score missing."""
    if home is None or away is None:
        return None
    if home > away:
        return 1
    if away > home:
        return 2
    return 3


def _parse_start(payload: dict) -> datetime | None:
    """Prefer ``strTimestamp`` (ISO 8601, naive UTC by TheSportsDB
    convention). Fall back to ``dateEvent`` + ``strTime`` for older
    rows that may not carry the timestamp field."""
    ts = payload.get("strTimestamp")
    if ts:
        try:
            return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    date = payload.get("dateEvent")
    if not date:
        return None
    time_str = payload.get("strTime") or "00:00:00"
    try:
        return datetime.fromisoformat(f"{date}T{time_str}").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _resolve_status(payload: dict, home_score, away_score) -> tuple[str, bool, bool]:
    """Map TheSportsDB ``strStatus`` / ``strPostponed`` onto the
    aggrigator's status vocabulary. Returns
    ``(status_type, is_finalized, completed)``.

    Status semantics (kept loose; the live-feed taxonomy is the
    source of truth for granular states — historical rows only need
    ``finished`` vs ``postponed`` vs ``unknown``):
      - Both scores present + status "Match Finished" -> finished/terminal.
      - strPostponed=="yes" -> postponed/non-terminal.
      - Anything else -> "unknown"/non-terminal (rare on a backfill;
        signals "we have a row from TheSportsDB but it isn't done").
    """
    if payload.get("strPostponed") == "yes":
        return "postponed", False, False
    if home_score is not None and away_score is not None:
        # All scored matches are finalized for backfill purposes — even
        # if strStatus is missing.
        return "finished", True, True
    return "unknown", False, False


def _team_spec_from_row(team: Team, score: int | None) -> TeamSpec:
    """Project a canonical Team DB row back into the TeamSpec shape so
    the existing upsert path can flow uniformly."""
    return TeamSpec(
        team_id=team.team_id,
        league_id=team.league_id,
        name_long=(team.canonical_name or team.name_long or team.team_id)[:128],
        name_medium=(team.name_medium or team.canonical_name or team.team_id)[:64],
        name_short=(team.name_short or team.canonical_name or team.team_id)[:32],
        primary_color=team.primary_color,
        secondary_color=team.secondary_color,
        primary_contrast=team.primary_contrast,
        secondary_contrast=team.secondary_contrast,
        stat_entity_id=team.stat_entity_id or "",
        score=score,
    )


# ---- top-level event translation -------------------------------------------


def event_spec_from_thesportsdb_payload(
    payload: dict,
    *,
    league_id: str,
    sport_id: str,
    home_team: Team,
    away_team: Team,
) -> EventSpec | None:
    """Translate one ``eventsround.php`` event payload to an ``EventSpec``.

    Returns ``None`` when essential identifiers are missing — the
    orchestrator drops + logs.

    ``home_team`` / ``away_team`` are pre-resolved canonical Team rows
    (looked up by ``thesportsdb_team_id``) — the translator never
    touches the DB.
    """
    raw_event_id = payload.get("idEvent")
    if not raw_event_id:
        logger.warning("TheSportsDB payload missing idEvent: %r", payload)
        return None

    start_time = _parse_start(payload)
    if start_time is None:
        logger.warning(
            "TheSportsDB payload %r missing start time", raw_event_id
        )
        return None

    home_score = _parse_score(payload.get("intHomeScore"))
    away_score = _parse_score(payload.get("intAwayScore"))
    winner_code = _winner_code(home_score, away_score)
    status_type, is_finalized, completed = _resolve_status(
        payload, home_score, away_score,
    )

    return EventSpec(
        event_id=f"tsdb:{raw_event_id}",
        sport_id=sport_id,
        league_id=league_id,
        type="match",
        season_label=payload.get("strSeason") or "",
        start_time=start_time,
        status_type=status_type,
        status_display=payload.get("strStatus") or "",
        current_period_id="",
        is_live=False,
        is_finalized=is_finalized,
        completed=completed,
        home_team=_team_spec_from_row(home_team, home_score),
        away_team=_team_spec_from_row(away_team, away_score),
        home_score=home_score,
        away_score=away_score,
        winner_code=winner_code,
        feed_locked=False,
        markets=[],
    )
