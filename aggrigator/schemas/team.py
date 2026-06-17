from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, computed_field


def team_logo_url(team_id: str, *, public_base: str) -> str:
    base = public_base.rstrip("/")
    return f"{base}/v1/teams/{team_id}/logo"


class TeamOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str                      # synthesized "{league_id}:{team_id}"
    public_id: uuid.UUID
    league_id: str
    team_id: str                 # raw provider teamID (or "_canon:<slug>" for filler rows)
    sport_id: str | None
    name_long: str
    name_medium: str
    name_short: str
    primary_color: str | None
    secondary_color: str | None
    primary_contrast: str | None
    secondary_contrast: str | None
    logo_url: str | None
    canonical_name: str
    odds_api_io_key: str | None = None
    thesportsdb_team_id: str | None = None
    match_confirmed: bool = False
    match_source: str | None = None

    @computed_field
    @property
    def name(self) -> str:
        """Alias for ``name_long`` — matches the legacy Django ``Team.name``
        property so portal templates that read ``team.name`` keep working."""
        return self.name_long
