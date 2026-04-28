"""Market — mirrors MDProject ``core_market``.

Enums kept as TEXT for parity (MDProject uses Django ``TextChoices`` which is a
string under the hood).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aggrigator.models.base import Base, TimestampMixin


class MarketCategory(StrEnum):
    MONEYLINE = "MONEYLINE"
    SPREAD = "SPREAD"
    TOTAL = "TOTAL"
    PROPS_GAME = "PROPS_GAME"
    PROPS_TEAM = "PROPS_TEAM"


class MarketScope(StrEnum):
    FULL_GAME = "FULL_GAME"
    H1 = "H1"
    H2 = "H2"
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q4 = "Q4"
    OVERTIME = "OVERTIME"
    PERIOD_N = "PERIOD_N"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    SHOOTOUT = "SHOOTOUT"
    INNING_1 = "INNING_1"
    INNING_2 = "INNING_2"
    INNING_3 = "INNING_3"
    INNING_4 = "INNING_4"
    INNING_5 = "INNING_5"
    INNING_6 = "INNING_6"
    INNING_7 = "INNING_7"
    INNING_8 = "INNING_8"
    INNING_9 = "INNING_9"
    INNINGS_1_5 = "INNINGS_1_5"
    INNINGS_1_3 = "INNINGS_1_3"
    MINUTES_5 = "MINUTES_5"
    MINUTES_10 = "MINUTES_10"


class Market(Base, TimestampMixin):
    __tablename__ = "core_market"
    __table_args__ = (
        Index("ix_core_market_event_id_category_scope", "event_id", "category", "scope"),
        Index("ix_core_market_sport_id_category_is_live", "sport_id", "category", "is_live"),
        Index("ix_core_market_event_id_type", "event_id", "type"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("core_event_event.id", ondelete="CASCADE"), nullable=False
    )
    sport_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("core_sport.id", ondelete="RESTRICT"), nullable=False
    )

    category: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(16), default=MarketScope.FULL_GAME.value)
    line: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    side: Mapped[str] = mapped_column(String(8), default="", server_default="")

    provider: Mapped[str] = mapped_column(
        String(32), default="sportsgameodds", server_default="sportsgameodds"
    )
    provider_market_id: Mapped[str] = mapped_column(
        String(128), default="", server_default="", index=True
    )
    provider_choice_group: Mapped[str] = mapped_column(
        String(16), default="", server_default=""
    )

    subject_team_id: Mapped[str | None] = mapped_column(
        String(80), ForeignKey("core_team.id", ondelete="SET NULL"), nullable=True
    )

    is_live: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    suspended: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    event = relationship("Event", back_populates="markets", lazy="raise")
    selections = relationship(
        "Selection", back_populates="market", cascade="all, delete-orphan", lazy="raise"
    )
