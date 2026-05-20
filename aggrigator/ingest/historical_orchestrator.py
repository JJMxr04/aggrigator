"""Historical events + final scores capture (TheSportsDB).

Pulls historical **scores** (not odds) for one league + one season
from TheSportsDB. See ``plans/analytics/TheSportsDB/`` for the design.

Flow:
  1. Gate: ``League.thesportsdb_id`` must be set AND
     ``League.can_pull_historical_scores=True`` (registry-recomputed
     by ``load_registry`` after Phase 1 / Phase 2). If either fails the
     orchestrator raises ``HistoricalIngestError`` — no API calls made.
  2. Resolve every Team in the league into a dict keyed by
     ``thesportsdb_team_id``.
  3. Walk rounds 1..N using TheSportsDB's ``eventsround.php`` — each
     call returns the full matchweek (10 fixtures for EPL, ~10 for
     MLS regular season + irregular openers / playoff brackets).
  4. For each event payload: translate to ``EventSpec`` and call
     ``upsert_event_from_spec(..., provider='thesportsdb')``. Skip
     ``write_markets`` entirely — TheSportsDB has no odds.
  5. Stop after ``empty_round_stop_threshold`` consecutive empty rounds
     (MLS has ~50 real rounds; EPL has 38; empties signal end of season).

Idempotent on re-run: ``upsert_event_from_spec`` SELECTs by ``event_id``
(``"tsdb:<idEvent>"``); re-running re-uses the same rows.

Provider stamp on Event rows: ``"thesportsdb"``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.ingest.thesportsdb_client import (
    TheSportsDbClient,
    TheSportsDbError,
)
from aggrigator.ingest.thesportsdb_translate import (
    event_spec_from_thesportsdb_payload,
)
from aggrigator.ingest.upserts import upsert_event_from_spec
from aggrigator.models import League, Team

logger = logging.getLogger(__name__)


PROVIDER = "thesportsdb"

# Upper bound on the rounds-loop. EPL has 38 rounds; MLS goes to ~50
# with playoffs. The orchestrator stops earlier via the consecutive-
# empties heuristic — this is just a hard cap so a misconfigured league
# can't loop indefinitely.
DEFAULT_MAX_ROUND = 60

# After this many consecutive empty rounds, assume the season is over
# and stop walking. ``eventsround.php`` returns ``"events": null`` for
# rounds past the schedule — usually just one terminating empty, but
# MLS has occasional gaps mid-season around all-star/break weeks.
DEFAULT_EMPTY_ROUND_STOP_THRESHOLD = 3


# ---- errors / report ------------------------------------------------------


class HistoricalIngestError(Exception):
    """Pre-flight failure (league not eligible, missing config). Raised
    before any API calls so it's free."""


@dataclass
class HistoricalIngestReport:
    league_id: str
    season_label: str
    rounds_fetched: int = 0
    events_upserted: int = 0
    events_skipped: int = 0  # team lookup miss or translator returned None
    duration_seconds: float = 0.0
    last_round: int | None = None  # the final round number actually fetched
    issues: list[str] = field(default_factory=list)


# ---- public entry point ----------------------------------------------------


async def ingest_historical_league(
    session: AsyncSession,
    league_id: str,
    season_label: str,
    *,
    client: TheSportsDbClient | None = None,
    rounds: Iterable[int] | None = None,
    empty_round_stop_threshold: int = DEFAULT_EMPTY_ROUND_STOP_THRESHOLD,
) -> HistoricalIngestReport:
    """Backfill one league + one season's worth of scored events.

    Arguments:
      - ``league_id``: aggrigator canonical (e.g. ``"EPL"``).
      - ``season_label``: TheSportsDB ``strSeason`` format. EPL uses
        ``"2024-2025"``; MLS uses ``"2024"``.
      - ``client``: pre-built client (lets tests inject a fixture
        client). If None, a default ``TheSportsDbClient`` is created
        and cleaned up at the end.
      - ``rounds``: explicit round numbers to fetch. Default walks
        ``1..DEFAULT_MAX_ROUND`` with early stop on empties — that's
        the right behavior for full-season backfill. Pass an explicit
        range to limit (smoke tests, partial backfills).
      - ``empty_round_stop_threshold``: when ``rounds`` is None (the
        auto-walk path), stop after this many consecutive empty
        responses. Ignored when ``rounds`` is explicit.

    Returns ``HistoricalIngestReport``. Always commits per round so a
    mid-walk failure doesn't lose progress.
    """
    start_wall = time.monotonic()
    report = HistoricalIngestReport(league_id=league_id, season_label=season_label)

    league = await _gate_or_raise(session, league_id)
    team_lookup = await _team_lookup(session, league_id, report)

    owns_client = client is None
    client = client or TheSportsDbClient()

    try:
        round_iter = _round_iter(rounds, empty_round_stop_threshold)
        consecutive_empty = 0
        for round_num in round_iter:
            events = await client.events_round(
                league.thesportsdb_id, round_num, season_label,
            )
            report.rounds_fetched += 1
            report.last_round = round_num

            if not events:
                consecutive_empty += 1
                if rounds is None and consecutive_empty >= empty_round_stop_threshold:
                    logger.info(
                        "thesportsdb: %s %s — %d consecutive empty rounds, "
                        "stopping at round %d",
                        league_id, season_label, consecutive_empty, round_num,
                    )
                    break
                continue
            consecutive_empty = 0

            for payload in events:
                upserted = await _ingest_one_event(
                    session, payload, league, team_lookup, report,
                )
                if upserted:
                    report.events_upserted += 1
                else:
                    report.events_skipped += 1

            await session.commit()
            logger.info(
                "thesportsdb: %s %s round %d -> %d events (cum: %d upserted, %d skipped)",
                league_id, season_label, round_num,
                len(events), report.events_upserted, report.events_skipped,
            )
    except TheSportsDbError as e:
        await session.rollback()
        report.issues.append(f"TheSportsDB error at round {report.last_round}: {e}")
        raise
    finally:
        if owns_client:
            await client.aclose()
        report.duration_seconds = round(time.monotonic() - start_wall, 1)

    logger.info(
        "thesportsdb done: %s %s rounds=%d events=%d skipped=%d in %.1fs",
        league_id, season_label, report.rounds_fetched,
        report.events_upserted, report.events_skipped, report.duration_seconds,
    )
    return report


# ---- pre-flight ------------------------------------------------------------


async def _gate_or_raise(session: AsyncSession, league_id: str) -> League:
    league = await session.get(League, league_id)
    if league is None:
        raise HistoricalIngestError(f"league {league_id!r} not in DB")
    if not league.thesportsdb_id:
        raise HistoricalIngestError(
            f"league {league_id!r} has no thesportsdb_id — run load_registry"
        )
    if not league.can_pull_historical_scores:
        raise HistoricalIngestError(
            f"league {league_id!r} not eligible: "
            f"can_pull_historical_scores is False (some teams missing "
            f"thesportsdb_team_id or match_confirmed=False)"
        )
    return league


async def _team_lookup(
    session: AsyncSession,
    league_id: str,
    report: HistoricalIngestReport,
) -> dict[str, Team]:
    """thesportsdb_team_id -> Team row, for the given league.

    Confirmed-eligible teams without a thesportsdb_team_id are a hard
    inconsistency — the gate should have caught it. Report it on the
    issues list anyway so a manual operator review surfaces."""
    teams = (await session.scalars(
        select(Team).where(Team.league_id == league_id)
    )).all()
    lookup: dict[str, Team] = {}
    for t in teams:
        if t.thesportsdb_team_id:
            lookup[t.thesportsdb_team_id] = t
        elif t.match_confirmed:
            report.issues.append(
                f"team {t.canonical_name!r} (id={t.id}) is match_confirmed "
                f"but missing thesportsdb_team_id"
            )
    return lookup


# ---- round walk ------------------------------------------------------------


def _round_iter(
    rounds: Iterable[int] | None,
    empty_round_stop_threshold: int,
) -> Iterable[int]:
    """Either the caller's explicit rounds or 1..DEFAULT_MAX_ROUND."""
    if rounds is not None:
        return iter(rounds)
    return iter(range(1, DEFAULT_MAX_ROUND + 1))


# ---- single-event ingest ---------------------------------------------------


async def _ingest_one_event(
    session: AsyncSession,
    payload: dict,
    league: League,
    team_lookup: dict[str, Team],
    report: HistoricalIngestReport,
) -> bool:
    """Translate + upsert one event. Returns True on success, False if
    skipped for any reason (missing team, missing fields)."""
    home_tsdb_id = payload.get("idHomeTeam")
    away_tsdb_id = payload.get("idAwayTeam")
    if not home_tsdb_id or not away_tsdb_id:
        logger.warning(
            "thesportsdb event %r missing idHomeTeam/idAwayTeam — skipping",
            payload.get("idEvent"),
        )
        return False

    home_team = team_lookup.get(home_tsdb_id)
    away_team = team_lookup.get(away_tsdb_id)
    if home_team is None or away_team is None:
        # Promotion/relegation edge case — a team in TheSportsDB's
        # roster for this season but not in our canonical registry.
        missing = []
        if home_team is None:
            missing.append(f"home={home_tsdb_id} ({payload.get('strHomeTeam')!r})")
        if away_team is None:
            missing.append(f"away={away_tsdb_id} ({payload.get('strAwayTeam')!r})")
        msg = (
            f"thesportsdb event {payload.get('idEvent')!r}: team(s) not in "
            f"registry: {', '.join(missing)}"
        )
        logger.warning(msg)
        report.issues.append(msg)
        return False

    spec = event_spec_from_thesportsdb_payload(
        payload,
        league_id=league.id,
        sport_id=league.sport_id,
        home_team=home_team,
        away_team=away_team,
    )
    if spec is None:
        return False

    upserted = await upsert_event_from_spec(
        session, spec,
        home=home_team,
        away=away_team,
        provider=PROVIDER,
    )
    return upserted is not None
