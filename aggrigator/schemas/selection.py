from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field

from aggrigator.ingest.converters import (
    decimal_to_american,
    decimal_to_fractional,
)


class OddsOut(BaseModel):
    """Odds in all three formats — computed server-side so every consumer
    (portal, Flutter, third-party) gets the same numbers without re-doing
    the conversion math."""
    decimal: Decimal
    american: int
    fractional: str


class BookmakerSelectionOut(BaseModel):
    """One bookmaker's current quote on a selection. ``bookmaker_name`` comes
    from the eager-loaded ``BookmakerSelection.bookmaker`` relationship — so
    portal/Flutter clients can render "DraftKings -150" without a second
    roundtrip to ``/v1/bookmakers``.

    ``odds`` is a convenience envelope mirroring ``SelectionOut.odds`` so the
    UI uses one consistent format for the fair price *and* every per-book
    price. ``None`` when the bookmaker has the line suspended."""
    model_config = ConfigDict(from_attributes=True)

    bookmaker_id: str
    bookmaker_name: str | None = None
    decimal_odds: Decimal | None
    spread: Decimal | None
    over_under: Decimal | None
    available: bool
    deeplink: str
    last_updated_at: datetime | None

    @computed_field
    @property
    def odds(self) -> OddsOut | None:
        if self.decimal_odds is None:
            return None
        return OddsOut(
            decimal=self.decimal_odds,
            american=decimal_to_american(self.decimal_odds),
            fractional=decimal_to_fractional(self.decimal_odds),
        )


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
    # Per-bookmaker quotes. Pydantic looks for ``by_book`` on the source SQLA
    # model (the relationship name) but emits the field as ``by_bookmaker`` in
    # API JSON — friendlier for portal/Flutter consumers. Empty list when the
    # caller didn't eager-load ``Selection.by_book`` (lazy="raise" otherwise).
    by_bookmaker: list[BookmakerSelectionOut] = Field(
        default_factory=list,
        validation_alias=AliasChoices("by_bookmaker", "by_book"),
    )

    @computed_field
    @property
    def odds(self) -> OddsOut | None:
        """Current odds in three formats. ``None`` when the line is suspended
        or unavailable (``decimal_odds is None``)."""
        if self.decimal_odds is None:
            return None
        return OddsOut(
            decimal=self.decimal_odds,
            american=decimal_to_american(self.decimal_odds),
            fractional=decimal_to_fractional(self.decimal_odds),
        )

    @computed_field
    @property
    def opening_odds(self) -> OddsOut | None:
        if self.opening_decimal_odds is None:
            return None
        return OddsOut(
            decimal=self.opening_decimal_odds,
            american=decimal_to_american(self.opening_decimal_odds),
            fractional=decimal_to_fractional(self.opening_decimal_odds),
        )


# Kept as an explicit alias for places that statically expect the name —
# functionally identical to SelectionOut now that books ship inline.
SelectionWithBooksOut = SelectionOut


class QuoteOut(BaseModel):
    decimal_odds: Decimal
    captured_at: datetime


class SelectionMovementOut(BaseModel):
    selection_id: str
    quotes: list[QuoteOut]
