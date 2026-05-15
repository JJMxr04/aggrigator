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

    # Default ``active=True`` — newly-seeded leagues are ON by default,
    # but the parent Sport row gates them. ``ingest_due_leagues`` walks
    # only leagues whose Sport is ALSO active, so a fresh DB still walks
    # zero leagues until an operator flips a sport on. Once the sport is
    # on, re-seeding pulls in its leagues already enabled. Re-seeding
    # does NOT reset operator-flipped league rows (per
    # ``upsert_league_from_spec``).
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    refresh_cadence_minutes: Mapped[int] = mapped_column(
        Integer, default=720, server_default="720"
    )
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    sport = relationship("Sport", back_populates="leagues")
