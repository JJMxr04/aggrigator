"""Stateless parlay combiner — selections in, combined-decimal out."""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, Field


class SlipLegIn(BaseModel):
    selection_id: str


class SlipsIn(BaseModel):
    legs: list[SlipLegIn] = Field(min_length=1, max_length=20)


class SlipLegOut(BaseModel):
    selection_id: str
    market_id: str
    type: str
    label: str
    decimal_odds: Decimal


class SlipsOut(BaseModel):
    slip_id: uuid.UUID
    legs: list[SlipLegOut]
    combined_decimal: Decimal
