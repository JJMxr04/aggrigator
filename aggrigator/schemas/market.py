from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from aggrigator.schemas.selection import SelectionOut
from aggrigator.schemas.team import TeamOut


class MarketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    event_id: str
    sport_id: str
    category: str
    type: str
    scope: str
    line: Decimal | None
    side: str
    provider: str
    provider_market_id: str
    subject_team_id: str | None
    # Pre-joined nested object (vs. just the FK string above). Used by per-team
    # prop markets (e.g. NHL_TEAM_TOTAL_GOALS where ``side="HOME"``).
    subject_team: TeamOut | None = None
    is_live: bool
    suspended: bool
    last_updated: datetime
    selections: list[SelectionOut] = []
