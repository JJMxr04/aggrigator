"""Shared filter models for the query layer.

One place for the window/CSV/Decimal parsing that used to be
copy-pasted across ``api/events.py`` / ``api/analytics.py``. Routes
construct these from their Query params; the query classes consume
them. Parse errors that must surface as HTTP 400 (not 422) raise
``ValueError`` — the route translates.
"""

from __future__ import annotations

from datetime import date as Date, datetime
from typing import Literal

from pydantic import BaseModel


class EventListFilters(BaseModel):
    """``GET /v1/events`` filters. Window resolution (date wins, then
    explicit bounds, then the 3h-ago→3-days-out default) happens in
    ``EventQueries.list_stmt`` so every consumer gets identical
    semantics."""
    sport: str | None = None
    league: str | None = None
    live: bool | None = None
    date: Date | None = None
    starts_after: datetime | None = None
    starts_before: datetime | None = None


class MarketFilters(BaseModel):
    """``GET /v1/events/{id}/markets`` filters. ``category``/``scope``
    are raw comma-separated strings (split in the query layer);
    ``min_decimal``/``max_decimal`` stay raw strings because their
    parse failure must be a 400 with the legacy message, not a 422."""
    category: str | None = None
    scope: str | None = None
    scope_subject: Literal["game", "team"] | None = None
    type: str | None = None
    live: bool | None = None
    team_id: str | None = None
    settled: bool | None = None
    min_decimal: str | None = None
    max_decimal: str | None = None
