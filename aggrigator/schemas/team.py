from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, computed_field


class TeamOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str                      # synthesized "{league_id}:{team_id}"
    public_id: uuid.UUID
    league_id: str
    team_id: str                 # raw SGO teamID
    sport_id: str | None
    name_long: str
    name_medium: str
    name_short: str
    primary_color: str | None
    secondary_color: str | None
    primary_contrast: str | None
    secondary_contrast: str | None
    logo_url: str | None

    @computed_field
    @property
    def name(self) -> str:
        """Alias for ``name_long`` — matches the legacy Django ``Team.name``
        property so portal templates that read ``team.name`` keep working."""
        return self.name_long
