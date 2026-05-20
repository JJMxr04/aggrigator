"""Bet — user-tracked wagers placed externally. New in Phase D
(see ``plans/analytics/dashboard_and_data/07-bet-tracking.md``).

The aggrigator does NOT place bets — every row here is user-entered,
manually or via the portal's entry form. The dashboard reads from this
table to produce the equity curve / hit rate / ROI display.

Single-user-per-tenant for v1 (matches the existing
``TenantUser`` shape). ``event_id`` is nullable so users can log bets
on markets the aggrigator doesn't track (player props, parlays).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from aggrigator.models.base import Base, TimestampMixin


class BetSettlementStatus(StrEnum):
    OPEN = "open"
    WON = "won"
    LOST = "lost"
    PUSH = "push"
    VOID = "void"


class BetSelectionType(StrEnum):
    """Closed vocabulary the auto-settle hook understands. ``OTHER``
    is the escape hatch for anything outside the moneyline path
    (props, parlays, custom markets) — these never auto-settle."""
    MONEYLINE_HOME = "moneyline_home"
    MONEYLINE_AWAY = "moneyline_away"
    MONEYLINE_DRAW = "moneyline_draw"
    TOTAL_OVER = "total_over"
    TOTAL_UNDER = "total_under"
    SPREAD_HOME = "spread_home"
    SPREAD_AWAY = "spread_away"
    OTHER = "other"


class Bet(Base, TimestampMixin):
    __tablename__ = "core_bet"
    __table_args__ = (
        Index("ix_core_bet_tenant_user_placed_at", "tenant_user_id", "placed_at"),
        Index(
            "ix_core_bet_tenant_user_settlement_status",
            "tenant_user_id", "settlement_status",
        ),
        Index("ix_core_bet_event_id_settlement_status", "event_id", "settlement_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_user.id", ondelete="CASCADE"),
        nullable=False,
    )

    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )

    # FK to Event. Nullable so a user can log a bet on a market we don't
    # track (player prop, exotic). When NULL, ``label`` is the only
    # human-readable handle; auto-settle never fires for these rows.
    event_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("core_event_event.id", ondelete="SET NULL"),
        nullable=True,
    )

    label: Mapped[str] = mapped_column(String(255), nullable=False)
    selection_type: Mapped[str] = mapped_column(String(32), nullable=False)
    bookmaker: Mapped[str | None] = mapped_column(String(64), nullable=True)

    stake: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    decimal_odds: Mapped[Decimal] = mapped_column(Numeric(7, 3), nullable=False)

    settlement_status: Mapped[str] = mapped_column(
        String(16),
        default=BetSettlementStatus.OPEN.value,
        server_default=BetSettlementStatus.OPEN.value,
        nullable=False,
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Final payout in the user's currency. ``stake * decimal_odds`` for a win,
    # ``stake`` for push or void, ``0`` for loss. Always None while ``open``.
    payout: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
