"""Bookmaker + BookmakerSelection — mirror MDProject ``core_bookmaker`` and
``core_bookmaker_selection``.

The fair-odds row on Selection is the canonical price. Per-book entries are
display-only — they power "DraftKings -150, FanDuel -145" UI.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aggrigator.models.base import Base, TimestampMixin


class Bookmaker(Base, TimestampMixin):
    __tablename__ = "core_bookmaker"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class BookmakerSelection(Base, TimestampMixin):
    __tablename__ = "core_bookmaker_selection"
    __table_args__ = (
        UniqueConstraint(
            "selection_id", "bookmaker_id",
            name="uq_core_bookmaker_selection_selection_id_bookmaker_id",
        ),
        Index(
            "ix_core_bookmaker_selection_selection_id_available",
            "selection_id", "available",
        ),
        Index(
            "ix_core_bookmaker_selection_bookmaker_id_available",
            "bookmaker_id", "available",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    selection_id: Mapped[str] = mapped_column(
        String(160),
        ForeignKey("core_selection.id", ondelete="CASCADE"),
        nullable=False,
    )
    bookmaker_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("core_bookmaker.id", ondelete="RESTRICT"),
        nullable=False,
    )

    decimal_odds: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    spread: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    over_under: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)

    available: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    deeplink: Mapped[str] = mapped_column(String(500), default="", server_default="")
    last_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    selection = relationship("Selection", back_populates="by_book", lazy="raise")
