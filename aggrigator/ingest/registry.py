"""Load the on-disk team/league/sport registry into the database.

Three top-level functions:

- :func:`load_registry_from_disk` reads the JSON tree under
  ``aggrigator/data/sports/`` and returns a typed :class:`RegistrySnapshot`.
  Per-file shape validation runs via pydantic; any malformed file raises.
- :func:`validate_registry` is a pure cross-file integrity check
  (canonical_id alignment, duplicate names, dangling references). Returns
  a list of human-readable issues — empty means clean.
- :func:`apply_registry_to_db` applies a validated snapshot to the DB:
  annotates ``Sport`` + ``League`` rows with provider keys, upserts
  ``Team`` rows by ``(league_id, canonical_name)``, then recomputes
  ``League.can_pull_historical_scores`` per league.

Re-runnable: the loader never destroys data. Existing Sport / League rows
keep their ``name`` / ``active`` flags (those are owned by ``seed.py`` and
the operator). Existing Team rows have their canonical metadata and
provider keys reset to whatever the registry says — the registry is
authoritative for cross-provider identity.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.models import League, Sport, Team
from aggrigator.schemas.registry import (
    LeagueEntry,
    LeaguesRegistryFile,
    SportEntry,
    SportsRegistryFile,
    TeamEntry,
    TeamsRegistryFile,
    key_or_none,
)

logger = logging.getLogger(__name__)

# Locate ``aggrigator/data/sports/`` relative to this file. Layout:
#   .../aggrigator/aggrigator/ingest/registry.py   <- __file__
#   .../aggrigator/aggrigator/data/sports/
_REGISTRY_ROOT = pathlib.Path(__file__).resolve().parent.parent / "data" / "sports"


# ---- snapshot -------------------------------------------------------------


@dataclass(frozen=True)
class RegistrySnapshot:
    """Parsed + per-file-validated registry. Pass to ``validate_registry``
    for cross-file checks before applying to the DB."""

    sports: list[SportEntry]
    leagues_by_sport: dict[str, list[LeagueEntry]]    # sport_canonical_id -> leagues
    teams_by_league: dict[str, list[TeamEntry]]       # league_canonical_id -> teams


@dataclass
class ApplyReport:
    sports_updated: int = 0
    leagues_updated: int = 0
    teams_inserted: int = 0
    teams_updated: int = 0
    leagues_marked_historical_eligible: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


# ---- load -----------------------------------------------------------------


def load_registry_from_disk(root: pathlib.Path | None = None) -> RegistrySnapshot:
    """Walk ``aggrigator/data/sports/`` and return a validated snapshot.

    Raises ``FileNotFoundError`` if any expected file is missing. Pydantic
    ``ValidationError`` for malformed shapes. Both surface clearly with
    the offending path in the message.
    """
    base = root if root is not None else _REGISTRY_ROOT
    sports_path = base / "sports.json"
    if not sports_path.exists():
        raise FileNotFoundError(
            f"registry root missing sports.json: {sports_path}"
        )

    sports_file = SportsRegistryFile.model_validate_json(sports_path.read_text())

    leagues_by_sport: dict[str, list[LeagueEntry]] = {}
    teams_by_league: dict[str, list[TeamEntry]] = {}

    for sport in sports_file.sports:
        sport_dir = base / sport.canonical_id / "leagues"
        leagues_path = sport_dir / f"{sport.canonical_id}-leagues.json"
        if not leagues_path.exists():
            raise FileNotFoundError(
                f"missing leagues file for sport={sport.canonical_id}: "
                f"{leagues_path}"
            )
        leagues_file = LeaguesRegistryFile.model_validate_json(
            leagues_path.read_text()
        )
        if leagues_file.sport_canonical_id != sport.canonical_id:
            raise ValueError(
                f"{leagues_path}: sport_canonical_id "
                f"{leagues_file.sport_canonical_id!r} does not match parent "
                f"sport {sport.canonical_id!r}"
            )
        leagues_by_sport[sport.canonical_id] = leagues_file.leagues

        for league in leagues_file.leagues:
            teams_path = sport_dir / f"{league.canonical_id}-teams.json"
            if not teams_path.exists():
                raise FileNotFoundError(
                    f"missing teams file for league={league.canonical_id}: "
                    f"{teams_path}"
                )
            teams_file = TeamsRegistryFile.model_validate_json(
                teams_path.read_text()
            )
            if teams_file.sport_canonical_id != sport.canonical_id:
                raise ValueError(
                    f"{teams_path}: sport_canonical_id mismatch"
                )
            if teams_file.league_canonical_id != league.canonical_id:
                raise ValueError(
                    f"{teams_path}: league_canonical_id mismatch"
                )
            teams_by_league[league.canonical_id] = teams_file.teams

    return RegistrySnapshot(
        sports=sports_file.sports,
        leagues_by_sport=leagues_by_sport,
        teams_by_league=teams_by_league,
    )


# ---- validate -------------------------------------------------------------


def validate_registry(snap: RegistrySnapshot) -> list[str]:
    """Cross-file integrity checks. Returns issues — empty list = clean."""
    issues: list[str] = []

    # 1. Every league has a teams entry.
    all_league_ids = {
        lg.canonical_id
        for leagues in snap.leagues_by_sport.values()
        for lg in leagues
    }
    missing_team_files = all_league_ids - set(snap.teams_by_league.keys())
    for lid in sorted(missing_team_files):
        issues.append(f"league {lid!r} has no teams file")

    # 2. Duplicate canonical names within a league.
    for league_id, teams in snap.teams_by_league.items():
        seen: dict[str, int] = {}
        for t in teams:
            seen[t.canonical_name] = seen.get(t.canonical_name, 0) + 1
        for name, n in seen.items():
            if n > 1:
                issues.append(
                    f"league {league_id!r}: canonical_name {name!r} "
                    f"appears {n} times"
                )

    # 3. Soft warning — active leagues with zero teams.
    for sport_id, leagues in snap.leagues_by_sport.items():
        for lg in leagues:
            if lg.active and not snap.teams_by_league.get(lg.canonical_id):
                issues.append(
                    f"league {lg.canonical_id!r} (sport {sport_id!r}) is "
                    f"active but has zero teams"
                )

    # 4. Duplicate canonical ids across sports / leagues.
    sport_ids = [s.canonical_id for s in snap.sports]
    if len(set(sport_ids)) != len(sport_ids):
        issues.append(
            f"duplicate sport canonical_id in sports.json: {sport_ids}"
        )
    for sport_id, leagues in snap.leagues_by_sport.items():
        ids = [lg.canonical_id for lg in leagues]
        if len(set(ids)) != len(ids):
            issues.append(
                f"sport {sport_id!r}: duplicate league canonical_id: {ids}"
            )

    return issues


# ---- apply ----------------------------------------------------------------


async def apply_registry_to_db(
    session: AsyncSession,
    snap: RegistrySnapshot,
) -> ApplyReport:
    """Write the registry into the DB. Idempotent on re-run."""
    report = ApplyReport()

    # ---- sports: annotate keys, never touch name / active --------------
    for entry in snap.sports:
        sport = await session.get(Sport, entry.canonical_id)
        if sport is None:
            # The registry is annotation-only — Sport rows are still
            # owned by ``seed_sports``. Skip-and-warn so a missed seed
            # surfaces as an issue instead of being silently papered
            # over with an empty Sport row.
            report.issues.append(
                f"sport {entry.canonical_id!r} not in DB — run seed_sports "
                f"before load_registry"
            )
            continue
        sport.odds_api_io_key = key_or_none(entry.odds_api_io_key)
        sport.thesportsdb_id = key_or_none(entry.thesportsdb_strSport)
        report.sports_updated += 1

    # ---- leagues: annotate keys, never touch name / active -------------
    for sport_id, leagues in snap.leagues_by_sport.items():
        for entry in leagues:
            league = await session.get(League, entry.canonical_id)
            if league is None:
                report.issues.append(
                    f"league {entry.canonical_id!r} not in DB — run "
                    f"seed_leagues before load_registry"
                )
                continue
            league.odds_api_io_key = key_or_none(entry.odds_api_io_key)
            league.thesportsdb_id = key_or_none(entry.thesportsdb_league_id)
            report.leagues_updated += 1

    await session.flush()

    # ---- teams: upsert by (league_id, canonical_name) ------------------
    for league_id, teams in snap.teams_by_league.items():
        league = await session.get(League, league_id)
        if league is None:
            # The above loop already reported this; skip team work.
            continue
        for entry in teams:
            inserted = await _upsert_team_from_entry(session, league, entry)
            if inserted:
                report.teams_inserted += 1
            else:
                report.teams_updated += 1

    await session.flush()

    # ---- recompute can_pull_historical_scores per league ----------------
    for league_id in snap.teams_by_league.keys():
        league = await session.get(League, league_id)
        if league is None:
            continue
        eligible = await _league_is_historical_eligible(session, league_id)
        league.can_pull_historical_scores = eligible
        if eligible:
            report.leagues_marked_historical_eligible.append(league_id)

    await session.flush()
    logger.info(
        "apply_registry_to_db: sports=%d leagues=%d teams=+%d/u%d eligible=%s issues=%d",
        report.sports_updated,
        report.leagues_updated,
        report.teams_inserted,
        report.teams_updated,
        report.leagues_marked_historical_eligible,
        len(report.issues),
    )
    return report


async def _upsert_team_from_entry(
    session: AsyncSession,
    league: League,
    entry: TeamEntry,
) -> bool:
    """Upsert one Team row from a registry entry. Returns True if inserted."""
    io_key = key_or_none(entry.odds_api_io_key)
    tsdb_key = key_or_none(entry.thesportsdb_team_id)

    # Look up by canonical_name first — that's the authoritative identity
    # the registry owns. A roster_filler row that later picks up an
    # io_key still matches on canonical_name.
    existing = await session.scalar(
        select(Team).where(
            Team.league_id == league.id,
            Team.canonical_name == entry.canonical_name,
        )
    )

    if existing is None and io_key is not None:
        # Fallback lookup: live-seeded rows pre-registry use ``team_id =
        # odds_api_io_key`` but may not have ``canonical_name`` yet
        # (migration 0010 backfills it from ``name_long``, which may not
        # match the registry's canonical_name exactly).
        existing = await session.scalar(
            select(Team).where(
                Team.league_id == league.id,
                Team.team_id == io_key,
            )
        )

    if existing is None:
        # Last-resort PK lookup. The two scoped lookups above can miss
        # (canonical_name diverged from the live-feed insert; team_id was
        # mutated by an earlier upsert) — but PK is deterministic from
        # (league_id, team_id) so an existing row at the same PK *will*
        # collide on insert. Reuse it instead of raising IntegrityError.
        candidate_team_id = io_key if io_key is not None else Team.synth_filler_team_id(
            entry.canonical_name
        )
        existing = await session.get(Team, Team.synth_pk(league.id, candidate_team_id))

    if existing is not None:
        existing.canonical_name = entry.canonical_name
        existing.odds_api_io_key = io_key
        existing.thesportsdb_team_id = tsdb_key
        existing.match_confirmed = entry.confirmed
        existing.match_source = entry.source
        # Keep team_id in sync with the io_key when present so the
        # legacy ``UQ(league_id, team_id)`` still indexes meaningfully.
        # For roster_filler rows (no io_key), leave team_id as whatever
        # synth value was there at insert.
        if io_key is not None and existing.team_id != io_key:
            existing.team_id = io_key
        return False

    # Insert new row.
    team_id = io_key if io_key is not None else Team.synth_filler_team_id(
        entry.canonical_name
    )
    pk = Team.synth_pk(league.id, team_id)
    row = Team(
        id=pk,
        league_id=league.id,
        team_id=team_id,
        sport_id=league.sport_id,
        name_long=entry.canonical_name[:128],
        name_medium=entry.canonical_name[:64],
        name_short=entry.canonical_name[:32],
        canonical_name=entry.canonical_name,
        odds_api_io_key=io_key,
        thesportsdb_team_id=tsdb_key,
        match_confirmed=entry.confirmed,
        match_source=entry.source,
    )
    session.add(row)
    return True


async def _league_is_historical_eligible(
    session: AsyncSession,
    league_id: str,
) -> bool:
    """True iff every team in the league has ``thesportsdb_team_id``
    non-null AND ``match_confirmed=true``.

    Note: ``odds_api_io_key`` is intentionally NOT required. Teams that
    were only ever in past seasons (e.g. relegated EPL clubs like Luton,
    Sheffield United) live in the registry to anchor historical events
    but have no odds-api.io presence anymore. The live-ingest path doesn't
    care about them; the historical path needs their TheSportsDB id
    to match incoming payloads."""
    offender = await session.scalar(
        select(Team.id).where(
            Team.league_id == league_id,
            (
                Team.thesportsdb_team_id.is_(None)
                | Team.match_confirmed.is_(False)
            ),
        ).limit(1)
    )
    return offender is None


# ---- convenience ----------------------------------------------------------


def report_as_dict(report: ApplyReport) -> dict:
    """For worker-task return values that go into ``cron_run.summary``."""
    return dataclasses.asdict(report)
