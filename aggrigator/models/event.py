"""Event — mirrors MDProject ``core_event_event``.

Aggregator-only deltas:
- ``id`` widened to ``VARCHAR(64)`` (MDProject uses 32; both sides expand — see
  plan §7.6).
- ``last_webhook_sent_hash`` / ``last_webhook_sent_at`` for outbound idempotency.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, SmallInteger, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aggrigator.models.base import Base, TimestampMixin


class Event(Base, TimestampMixin):
    __tablename__ = "core_event_event"
    __table_args__ = (
        Index(
            "ix_core_event_event_league_status_start",
            "league_id", "status_type", "start_time",
        ),
        Index(
            "ix_core_event_event_sport_status_start",
            "sport_id", "status_type", "start_time",
        ),
        Index("ix_core_event_event_start_time", "start_time"),
        Index("ix_core_event_event_status_type", "status_type"),
        Index("ix_core_event_event_is_live", "is_live"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), default=uuid.uuid4, unique=True, index=True, nullable=False
    )

    sport_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("core_sport.id", ondelete="SET NULL"), nullable=True
    )
    league_id: Mapped[str | None] = mapped_column(
        String(48), ForeignKey("core_league.id", ondelete="SET NULL"), nullable=True
    )

    type: Mapped[str] = mapped_column(String(16), default="", server_default="")
    season_label: Mapped[str] = mapped_column(String(64), default="", server_default="")

    start_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    status_type: Mapped[str] = mapped_column(String(32), default="", server_default="")
    status_display: Mapped[str] = mapped_column(String(64), default="", server_default="")
    current_period_id: Mapped[str] = mapped_column(String(16), default="", server_default="")
    is_live: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_finalized: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    completed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    home_team_id: Mapped[str | None] = mapped_column(
        String(80), ForeignKey("core_team.id", ondelete="SET NULL"), nullable=True
    )
    away_team_id: Mapped[str | None] = mapped_column(
        String(80), ForeignKey("core_team.id", ondelete="SET NULL"), nullable=True
    )

    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    winner_code: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    winner: Mapped[str | None] = mapped_column(String(255), nullable=True)

    feed_locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    last_provider_refresh_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Aggregator-only — outbound webhook idempotency state.
    last_webhook_sent_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_webhook_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    home_team = relationship("Team", foreign_keys=[home_team_id], lazy="raise")
    away_team = relationship("Team", foreign_keys=[away_team_id], lazy="raise")
    sport = relationship("Sport", lazy="raise")
    league = relationship("League", lazy="raise")
    markets = relationship(
        "Market", back_populates="event", cascade="all, delete-orphan", lazy="raise"
    )
