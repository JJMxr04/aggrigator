"""Selection — mirrors MDProject ``core_selection``.

PK is synthesized: ``f"{market_id}:{statEntityID}-{sideID}"``. Same scheme
MDProject uses so the upsert key is identical on both sides.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aggrigator.models.base import Base, TimestampMixin


class SelectionType(StrEnum):
    HOME = "HOME"
    AWAY = "AWAY"
    DRAW = "DRAW"
    OVER = "OVER"
    UNDER = "UNDER"
    YES = "YES"
    NO = "NO"
    EVEN = "EVEN"
    ODD = "ODD"
    DC_1X = "1X"
    DC_X2 = "X2"
    DC_12 = "12"
    NO_GOAL = "NO_GOAL"
    CUSTOM = "CUSTOM"


class SettlementStatus(StrEnum):
    PENDING = "PENDING"
    WON = "WON"
    LOST = "LOST"
    PUSH = "PUSH"
    VOID = "VOID"


class SettlementSource(StrEnum):
    PROVIDER = "PROVIDER"
    COMPUTED = "COMPUTED"
    MANUAL = "MANUAL"


class Selection(Base, TimestampMixin):
    __tablename__ = "core_selection"
    __table_args__ = (
        Index("ix_core_selection_market_id_type", "market_id", "type"),
        Index(
            "ix_core_selection_settlement_status_market_id",
            "settlement_status", "market_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    market_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("core_market.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    suspended: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    decimal_odds: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    opening_decimal_odds: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    movement: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")

    settlement_status: Mapped[str] = mapped_column(
        String(8),
        default=SettlementStatus.PENDING.value,
        server_default=SettlementStatus.PENDING.value,
        index=True,
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    settlement_source: Mapped[str] = mapped_column(
        String(16), default="", server_default=""
    )

    market = relationship("Market", back_populates="selections", lazy="raise")
    by_book = relationship(
        "BookmakerSelection",
        back_populates="selection",
        cascade="all, delete-orphan",
        lazy="raise",
    )
