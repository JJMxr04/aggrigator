"""Sport — mirrors MDProject ``core_sport``."""

from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aggrigator.models.base import Base, TimestampMixin


class Sport(Base, TimestampMixin):
    __tablename__ = "core_sport"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    short_name: Mapped[str] = mapped_column(String(48), default="", server_default="")
    # Default ``active=True`` — newly-seeded sports are walkable out of the
    # box. To restrict the cron's footprint, flip individual sports off in
    # SQLAdmin after the most recent seed (your decision persists until the
    # next seed run, per ``upsert_sport_from_sgo``).
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    leagues = relationship("League", back_populates="sport")
