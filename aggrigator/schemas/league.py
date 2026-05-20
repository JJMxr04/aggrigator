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
    odds_api_io_key: str | None = None
    thesportsdb_id: str | None = None
    can_pull_historical_scores: bool = False
