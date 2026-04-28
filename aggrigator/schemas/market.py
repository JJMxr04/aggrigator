from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from aggrigator.schemas.selection import SelectionOut


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
    is_live: bool
    suspended: bool
    last_updated: datetime
    selections: list[SelectionOut] = []
