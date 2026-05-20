"""Pydantic schemas for the bet-tracking endpoints."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aggrigator.models.bet import BetSelectionType, BetSettlementStatus


class BetOut(BaseModel):
    """Bet row as returned by the list / get endpoints. Mirrors the
    ``core_bet`` table 1:1 — analytics derivations (profit, hit rate)
    are computed by the dashboard, not embedded here."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    placed_at: datetime
    event_id: str | None
    label: str
    selection_type: str
    bookmaker: str | None
    stake: Decimal
    decimal_odds: Decimal
    settlement_status: str
    settled_at: datetime | None
    payout: Decimal | None
    note: str | None
    created: datetime
    updated: datetime


class BetCreate(BaseModel):
    """Body for ``POST /v1/analytics/bets``. ``placed_at`` defaults to
    now() server-side when omitted so the user doesn't have to fill the
    timestamp on every entry."""
    label: str = Field(..., min_length=1, max_length=255)
    selection_type: str = Field(..., max_length=32)
    stake: Decimal = Field(..., gt=0)
    decimal_odds: Decimal = Field(..., ge=Decimal("1.01"))
    placed_at: datetime | None = None
    event_id: str | None = Field(default=None, max_length=64)
    bookmaker: str | None = Field(default=None, max_length=64)
    note: str | None = None

    @field_validator("selection_type")
    @classmethod
    def _validate_selection_type(cls, v: str) -> str:
        allowed = {t.value for t in BetSelectionType}
        if v not in allowed:
            raise ValueError(f"selection_type must be one of {sorted(allowed)}")
        return v


class BetUpdate(BaseModel):
    """Body for ``PATCH /v1/analytics/bets/{id}``. All fields optional —
    the user typically only flips ``settlement_status`` (for manual
    settle of OTHER bets) or edits the note."""
    settlement_status: str | None = None
    payout: Decimal | None = None
    note: str | None = None
    settled_at: datetime | None = None

    @field_validator("settlement_status")
    @classmethod
    def _validate_settlement_status(cls, v: str | None) -> str | None:
        if v is None:
            return v
        allowed = {s.value for s in BetSettlementStatus}
        if v not in allowed:
            raise ValueError(f"settlement_status must be one of {sorted(allowed)}")
        return v
