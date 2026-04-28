"""OddsQuote — mirrors MDProject ``core_odds_quote``.

Append-only line-movement history. One row per real price change.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column


from aggrigator.models.base import Base


class OddsQuote(Base):
    __tablename__ = "core_odds_quote"
    __table_args__ = (
        UniqueConstraint(
            "selection_id", "captured_at",
            name="uq_core_odds_quote_selection_id_captured_at",
        ),
        Index("ix_core_odds_quote_selection_id_captured_at", "selection_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    selection_id: Mapped[str] = mapped_column(
        String(160), ForeignKey("core_selection.id", ondelete="CASCADE"), nullable=False
    )
    decimal_odds: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
