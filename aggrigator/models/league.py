"""League — mirrors MDProject ``core_league``."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aggrigator.models.base import Base, TimestampMixin


class League(Base, TimestampMixin):
    __tablename__ = "core_league"
    __table_args__ = (
        Index("ix_core_league_sport_id_active", "sport_id", "active"),
    )

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    sport_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("core_sport.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    short_name: Mapped[str] = mapped_column(String(48), default="", server_default="")

    # Default ``active=False`` — newly-seeded leagues are off until the
    # operator explicitly enables them in SQLAdmin. ``ingest_due_leagues``
    # filters on this, so a fresh DB walks zero leagues until you turn
    # something on. Re-seeding does NOT reset operator choices (per
    # ``upsert_league_from_sgo``).
    active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    refresh_cadence_minutes: Mapped[int] = mapped_column(
        Integer, default=720, server_default="720"
    )
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    sport = relationship("Sport", back_populates="leagues")
