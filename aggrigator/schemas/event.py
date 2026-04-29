from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from aggrigator.schemas.league import LeagueOut
from aggrigator.schemas.market import MarketOut
from aggrigator.schemas.sport import SportOut
from aggrigator.schemas.team import TeamOut


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    public_id: uuid.UUID
    sport_id: str | None
    league_id: str | None
    type: str
    season_label: str
    start_time: datetime | None
    status_type: str
    status_display: str
    is_live: bool
    is_finalized: bool
    completed: bool
    home_score: int | None
    away_score: int | None
    winner_code: int | None
    winner: str | None
    home_team: TeamOut | None
    away_team: TeamOut | None
    # Pre-joined nested objects (vs. just the FK strings above) so consumers
    # don't need follow-up calls to /v1/sports or /v1/leagues for the names.
    sport: SportOut | None = None
    league: LeagueOut | None = None


class EventWithMarketsOut(EventOut):
    markets: list[MarketOut] = []


class OddsMeta(BaseModel):
    """Per plan §3.4: surfaces freshness state without inline SGO calls."""
    stale: bool = False
    last_provider_refresh_at: datetime | None = None


class EventDetailOut(EventWithMarketsOut):
    odds_meta: OddsMeta = OddsMeta()
