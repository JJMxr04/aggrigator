from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class BookmakerSelectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    bookmaker_id: str
    decimal_odds: Decimal | None
    spread: Decimal | None
    over_under: Decimal | None
    available: bool
    deeplink: str
    last_updated_at: datetime | None


class SelectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    market_id: str
    type: str
    label: str
    suspended: bool
    decimal_odds: Decimal | None
    opening_decimal_odds: Decimal | None
    movement: int
    settlement_status: str
    settlement_source: str
    settled_at: datetime | None


class SelectionWithBooksOut(SelectionOut):
    by_bookmaker: list[BookmakerSelectionOut] = []


class QuoteOut(BaseModel):
    decimal_odds: Decimal
    captured_at: datetime


class SelectionMovementOut(BaseModel):
    selection_id: str
    quotes: list[QuoteOut]
