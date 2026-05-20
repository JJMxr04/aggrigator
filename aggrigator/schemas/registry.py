"""Pydantic shapes for the on-disk registry under ``aggrigator/data/sports/``.

The registry is the authoritative source for cross-provider key mapping.
Operators edit the JSON files on disk; the loader
(``aggrigator.ingest.registry``) reads them through these models, validates,
and pushes the result into ``core_sport`` / ``core_league`` / ``core_team``.

A field typed ``KeyOrFalse`` is either the provider's identifier string OR
the JSON literal ``false`` (meaning "no key on this provider yet"). The
loader converts ``False`` → ``None`` before writing to the DB.

``extra="forbid"`` everywhere — unknown keys in the JSON files are a
hard error so typos surface during the validate step instead of being
silently dropped.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

KeyOrFalse = str | Literal[False]
MatchSource = Literal["fuzzy_match", "manual_override", "roster_filler"]


# ---- sports.json ------------------------------------------------------------


class SportEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_id: str
    canonical_name: str
    odds_api_io_key: KeyOrFalse
    thesportsdb_strSport: KeyOrFalse
    confirmed: bool
    active: bool


class SportsRegistryFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    sports: list[SportEntry]


# ---- <SPORT>/leagues/<SPORT>-leagues.json -----------------------------------


class LeagueEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_id: str
    canonical_name: str
    odds_api_io_key: KeyOrFalse
    thesportsdb_league_id: KeyOrFalse
    confirmed: bool
    active: bool


class LeaguesRegistryFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    sport_canonical_id: str
    leagues: list[LeagueEntry]


# ---- <SPORT>/leagues/<LEAGUE>-teams.json ------------------------------------


class TeamEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str
    odds_api_io_key: KeyOrFalse
    thesportsdb_team_id: KeyOrFalse
    confirmed: bool
    source: MatchSource


class TeamsRegistryFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    sport_canonical_id: str
    league_canonical_id: str
    teams: list[TeamEntry]


# ---- helpers ----------------------------------------------------------------


def key_or_none(value: KeyOrFalse) -> str | None:
    """Translate the JSON-on-disk ``false`` sentinel to a DB-friendly NULL.

    Anything else (a non-empty string) round-trips as-is. An empty string
    would also become ``None`` since "" is not a meaningful provider key.
    """
    if value is False or value == "":
        return None
    return value
