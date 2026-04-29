"""Async upserts for Sport / League / Team / Event from SGO specs.

Mirrors the model-manager methods in MDProject (``Sport.objects.upsert_from_sgo``,
``Team.objects.upsert_from_spec``, ``Event.objects.upsert_from_spec``) but in
async SQLAlchemy 2.0. Each helper takes a session and a spec/payload, performs
get-or-create-or-update, returns the row.

For Event upserts we also return the *previous* state so the caller can
hand it to ``lifecycle.decide_transition`` without re-reading the row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.ingest.lifecycle import EventState
from aggrigator.ingest.normalize import EventSpec, TeamSpec
from aggrigator.models import Event, League, Sport, Team


# ---- sport / league (from /sports + /leagues SGO endpoints) ---------------


async def upsert_sport_from_sgo(session: AsyncSession, payload: dict) -> Sport | None:
    sport_id = payload.get("sportID")
    if not sport_id:
        return None
    row = await session.get(Sport, sport_id)
    if row is None:
        row = Sport(
            id=sport_id,
            name=payload.get("name") or sport_id.title(),
            short_name=(payload.get("shortName") or "")[:48],
            active=True,
        )
        session.add(row)
    else:
        row.name = payload.get("name") or sport_id.title()
        row.short_name = (payload.get("shortName") or "")[:48]
        # Every seed resets ``active=True``. Treat seed as "reset to known
        # default state" — the default being "everything SGO ships is on".
        # To deactivate a sport, flip it off in SQLAdmin AFTER the most
        # recent seed; your decision persists until the next seed run.
        row.active = True
    return row


async def upsert_league_from_sgo(session: AsyncSession, payload: dict) -> League | None:
    league_id = payload.get("leagueID")
    sport_id = payload.get("sportID")
    if not league_id or not sport_id:
        return None
    sport = await session.get(Sport, sport_id)
    if sport is None:
        sport = Sport(id=sport_id, name=sport_id.title(), active=True)
        session.add(sport)
        await session.flush()

    row = await session.get(League, league_id)
    if row is None:
        row = League(
            id=league_id,
            sport_id=sport.id,
            name=payload.get("name") or league_id,
            short_name=(payload.get("shortName") or "")[:48],
            active=True,
        )
        session.add(row)
    else:
        row.sport_id = sport.id
        row.name = payload.get("name") or league_id
        row.short_name = (payload.get("shortName") or "")[:48]
        # Same "reset to active" rule as ``upsert_sport_from_sgo`` —
        # ``ingest_due_leagues`` walks ``active=True`` leagues only, so the
        # natural expectation is that ``Run seed`` (or ``Run full_refresh``)
        # gets the operator back to "every league walkable" each time.
        row.active = True
    return row


# ---- team / event (from event spec produced by normalize) ------------------


async def upsert_team_from_spec(
    session: AsyncSession, spec: TeamSpec
) -> Team | None:
    if not spec.team_id or not spec.league_id:
        return None
    league = await session.get(League, spec.league_id)
    if league is None:
        return None
    pk = Team.synth_pk(spec.league_id, spec.team_id)
    row = await session.get(Team, pk)
    payload = dict(
        league_id=spec.league_id,
        team_id=spec.team_id,
        sport_id=league.sport_id,
        name_long=(spec.name_long or spec.team_id)[:128],
        name_medium=(spec.name_medium or spec.team_id)[:64],
        name_short=(spec.name_short or spec.team_id)[:32],
        primary_color=spec.primary_color,
        secondary_color=spec.secondary_color,
        primary_contrast=spec.primary_contrast,
        secondary_contrast=spec.secondary_contrast,
        stat_entity_id=(spec.stat_entity_id or "")[:8],
    )
    if row is None:
        row = Team(id=pk, **payload)
        session.add(row)
    else:
        for k, v in payload.items():
            setattr(row, k, v)
    return row


def _winner_name(winner_code: int | None, home: Team | None, away: Team | None) -> str | None:
    if winner_code == 1 and home:
        return home.name_long
    if winner_code == 2 and away:
        return away.name_long
    if winner_code == 3:
        return "Draw"
    return None


class UpsertedEvent(NamedTuple):
    """What ``upsert_event_from_spec`` returns to its caller."""
    event: Event
    previous_state: EventState | None


async def upsert_event_from_spec(
    session: AsyncSession,
    spec: EventSpec,
    *,
    home: Team | None,
    away: Team | None,
) -> UpsertedEvent:
    """Get-or-update an Event, returning the row and the *previous* state for
    transition decisions (mirrors MDProject's ``_should_settle`` / `_should_reopen``
    snapshot pattern).
    """
    league = await session.get(League, spec.league_id) if spec.league_id else None
    sport_id = (league.sport_id if league else None) or spec.sport_id

    existing = await session.get(Event, spec.event_id)
    previous_state: EventState | None = None
    if existing is not None:
        previous_state = EventState(
            status_type=existing.status_type,
            home_score=existing.home_score,
            away_score=existing.away_score,
        )

    payload = dict(
        league_id=league.id if league else None,
        sport_id=sport_id,
        type=(spec.type or "")[:16],
        season_label=(spec.season_label or "")[:64],
        start_time=spec.start_time,
        status_type=(spec.status_type or "")[:32],
        status_display=(spec.status_display or "")[:64],
        current_period_id=(spec.current_period_id or "")[:16],
        is_live=bool(spec.is_live),
        is_finalized=bool(spec.is_finalized),
        completed=bool(spec.completed),
        home_team_id=home.id if home else None,
        away_team_id=away.id if away else None,
        home_score=spec.home_score,
        away_score=spec.away_score,
        winner_code=spec.winner_code,
        winner=_winner_name(spec.winner_code, home, away),
        feed_locked=bool(spec.feed_locked),
        last_provider_refresh_at=datetime.now(tz=timezone.utc),
    )

    if existing is None:
        existing = Event(id=spec.event_id, **payload)
        session.add(existing)
    else:
        for k, v in payload.items():
            setattr(existing, k, v)

    return UpsertedEvent(event=existing, previous_state=previous_state)
