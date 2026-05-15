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
    # Default ``active=False`` — newly-seeded sports are off until the
    # operator explicitly enables them in SQLAdmin. Re-seeding does NOT
    # reset operator choices (per ``upsert_sport_from_spec``).
    active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    leagues = relationship("League", back_populates="sport")
