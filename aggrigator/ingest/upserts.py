"""Async upserts for Sport / League / Team / Event from the provider specs.

Mirrors the model-manager methods in MDProject (``Sport.objects.upsert_from_spec``,
``Team.objects.upsert_from_spec``, ``Event.objects.upsert_from_spec``) but in
async SQLAlchemy 2.0. Each helper takes a session and a spec/payload, performs
get-or-create-or-update, returns the row.

For Event upserts we also return the *previous* state so the caller can
hand it to ``lifecycle.decide_transition`` without re-reading the row.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.ingest.lifecycle import TERMINAL_STATUSES, EventState
from aggrigator.ingest.normalize import EventSpec, TeamSpec
from aggrigator.models import Event, League, Sport, Team

logger = logging.getLogger(__name__)


async def enqueue_logo_fetch(team_pk: str) -> None:
    """Best-effort enqueue of a logo fetch for a new team. Never raises into
    the ingest path — a queue hiccup must not fail event ingest."""
    try:
        from aggrigator.workers.tasks.logos import fetch_team_logo_task

        await fetch_team_logo_task.defer_async(team_pk=team_pk)
    except Exception as exc:  # noqa: BLE001
        logger.warning("logo enqueue failed for %s: %s", team_pk, exc)


# ---- sport / league (from /sports + /leagues provider endpoints) ---------------


async def upsert_sport_from_spec(session: AsyncSession, payload: dict) -> Sport | None:
    sport_id = payload.get("sportID")
    if not sport_id:
        return None
    row = await session.get(Sport, sport_id)
    if row is None:
        row = Sport(
            id=sport_id,
            name=payload.get("name") or sport_id.title(),
            short_name=(payload.get("shortName") or "")[:48],
            active=False,
        )
        session.add(row)
    else:
        row.name = payload.get("name") or sport_id.title()
        row.short_name = (payload.get("shortName") or "")[:48]
        # Re-seed preserves operator choices: the active flag is set when
        # the row is first inserted (default False) and never overwritten
        # by subsequent seeds. Operators flip sports on in SQLAdmin and
        # those decisions stick across daily seeds.
        # The provider-key columns (odds_api_io_key, thesportsdb_id)
        # are owned by the registry loader, not the seed feed — leave
        # them untouched here.
    return row


async def upsert_league_from_spec(session: AsyncSession, payload: dict) -> League | None:
    league_id = payload.get("leagueID")
    sport_id = payload.get("sportID")
    if not league_id or not sport_id:
        return None
    sport = await session.get(Sport, sport_id)
    if sport is None:
        sport = Sport(id=sport_id, name=sport_id.title(), active=False)
        session.add(sport)
        await session.flush()

    row = await session.get(League, league_id)
    if row is None:
        row = League(
            id=league_id,
            sport_id=sport.id,
            name=payload.get("name") or league_id,
            short_name=(payload.get("shortName") or "")[:48],
            active=False,
        )
        session.add(row)
    else:
        row.sport_id = sport.id
        row.name = payload.get("name") or league_id
        row.short_name = (payload.get("shortName") or "")[:48]
        # Re-seed preserves operator choices: a league an operator flipped
        # on stays on across daily seeds. New leagues land active=False —
        # the operator must enable them, and even then they're gated by
        # their parent Sport's ``active`` flag (``ingest_due_leagues``
        # requires both).
        # Registry-managed columns are NOT touched here:
        #   odds_api_io_key, thesportsdb_id, can_pull_historical_scores
        # — those are owned by the registry loader.
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

    # Registry-aware fallback: a roster_filler row inserted by
    # ``load_registry`` lives at PK ``{league}:_canon:<slug>`` but its
    # canonical_name matches the live payload's name_long. Reuse that
    # row instead of inserting a duplicate. We keep the row's PK stable
    # — only odds_api_io_key + team_id get the real provider id.
    if row is None:
        row = await session.scalar(
            select(Team).where(
                Team.league_id == spec.league_id,
                Team.canonical_name == (spec.name_long or spec.team_id),
            )
        )

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
        # Brand-new team — populate canonical_name + odds_api_io_key
        # in the same shot so the registry-aware lookup above hits on
        # the next live-feed pass.
        row = Team(
            id=pk,
            canonical_name=(spec.name_long or spec.team_id)[:128],
            odds_api_io_key=spec.team_id,
            **payload,
        )
        session.add(row)
        await enqueue_logo_fetch(pk)
    else:
        for k, v in payload.items():
            setattr(row, k, v)
        # If this is the first time the live feed has surfaced an
        # odds-api.io id for this canonical team, fill it in. Don't
        # overwrite an existing key — registry is authoritative.
        # A NULL/empty -> real transition also means we've never had a
        # provider key to fetch a crest with, so enqueue the logo fetch
        # now (keyed on the row's stable PK, which for a roster-filler
        # row differs from synth_pk).
        if not row.odds_api_io_key:
            row.odds_api_io_key = spec.team_id
            await enqueue_logo_fetch(row.id)
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
    skip_if_new_terminal: bool = False,
    skip_if_unknown_event: bool = False,
    provider: str = "odds_api_io",
) -> UpsertedEvent | None:
    """Get-or-update an Event, returning the row and the *previous* state for
    transition decisions (mirrors MDProject's ``_should_settle`` / `_should_reopen``
    snapshot pattern).

    When ``skip_if_new_terminal=True`` and the event isn't in our DB yet,
    Provider payloads in a terminal status (``finished`` / ``postponed`` /
    ``canceled``) are skipped — no row inserted, no markets written, no
    settlement attempted. Returns ``None`` to signal the skip. Cron
    callers (``ingest_due_leagues``) opt into this so we don't burn
    storage + entity quota on games we never knew were happening. Manual
    backfill paths (``/v1/admin/crons/ingest-event``) leave it off so an
    operator can explicitly request a finished game.

    When ``skip_if_unknown_event=True``, ANY event we haven't already
    inserted is skipped — used by the intra-day refresh-only walk so new
    events only appear via the once-a-day discovery cron. Independent of
    ``skip_if_new_terminal``: the discovery walk uses one, the refresh
    walk uses the other.

    ``provider`` is stamped onto the Event row so historical
    (TheSportsDB) rows can be filtered cleanly from live (odds-api.io)
    ones. The two providers have disjoint event_id namespaces
    (TheSportsDB ids are prefixed ``tsdb:``), so there's no PK
    collision risk — this column is the provenance marker, not a
    uniqueness key.
    """
    league = await session.get(League, spec.league_id) if spec.league_id else None
    sport_id = (league.sport_id if league else None) or spec.sport_id

    existing = await session.get(Event, spec.event_id)
    if existing is None and skip_if_unknown_event:
        logger.debug(
            "skipping unknown event %s — refresh-only walk, "
            "new events arrive on the daily discovery cron",
            spec.event_id,
        )
        return None
    if existing is None and skip_if_new_terminal:
        if (spec.status_type or "").lower() in TERMINAL_STATUSES:
            logger.debug(
                "skipping new terminal event %s (status=%s) — "
                "not in DB and the provider reports it as already over",
                spec.event_id, spec.status_type,
            )
            return None

    previous_state: EventState | None = None
    if existing is not None:
        previous_state = EventState(
            status_type=existing.status_type,
            home_score=existing.home_score,
            away_score=existing.away_score,
        )

    now = datetime.now(tz=timezone.utc)
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
        last_provider_refresh_at=now,
        # Reset on every successful upsert. The watchdog's disappearance
        # branch reads this to detect events that stop appearing in
        # /events.
        last_seen_upstream_at=now,
        provider=provider,
    )

    if existing is None:
        existing = Event(id=spec.event_id, **payload)
        session.add(existing)
    else:
        for k, v in payload.items():
            setattr(existing, k, v)

    return UpsertedEvent(event=existing, previous_state=previous_state)
