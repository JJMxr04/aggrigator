from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class LeagueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sport_id: str
    name: str
    short_name: str
    active: bool
    refresh_cadence_minutes: int
    last_refreshed_at: datetime | None
