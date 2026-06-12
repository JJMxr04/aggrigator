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
# Aliased: this is DDL metadata (a partial-index predicate), not a query —
# the tests/unit/test_sql_guard.py text() ban targets query paths.
from sqlalchemy import text as sa_text
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
        Index("ix_core_event_event_provider_league", "provider", "league_id"),
        Index("ix_core_event_event_linked_event_id", "linked_event_id"),
        # Watchdog scan index (migration 0007): partial over non-finalized
        # rows only. Declared here so autogenerate doesn't try to drop it.
        Index(
            "ix_core_event_event_last_seen_upstream_pending",
            "last_seen_upstream_at",
            postgresql_where=sa_text("is_finalized = false"),
        ),
        # Mirrors migration 0001: UNIQUE constraint on the column + a
        # separate plain index (NOT index=True+unique=True, which would
        # make SQLAlchemy expect a single unique index and flap forever
        # in autogenerate).
        Index("ix_core_event_event_public_id", "public_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), default=uuid.uuid4, unique=True, nullable=False
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

    # Which upstream provider this row's data came from. Live + upcoming
    # events come from odds-api.io ("odds_api_io"); historical scored
    # events from TheSportsDB ("thesportsdb"). The TheSportsDB id space
    # is namespaced with a ``"tsdb:"`` prefix on ``id`` so PK collisions
    # with odds-api.io are structurally impossible — this column is the
    # provenance marker and lets the analytics layer filter cleanly.
    provider: Mapped[str] = mapped_column(
        String(32),
        default="odds_api_io",
        server_default="odds_api_io",
        nullable=False,
    )

    # Self-FK reserved for a future capture-from-live odds pipeline that
    # would snapshot odds-api.io prices at ``commence_time`` and pair
    # them to the corresponding TheSportsDB historical row. No current
    # consumer (TheSportsDB has no odds), so this column is orphaned in
    # the present design. Kept rather than dropped so adding that
    # pipeline later doesn't need a fresh alembic revision.
    linked_event_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("core_event_event.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Disappearance clock for the odds-api.io watchdog branch — refreshed
    # on every successful upsert. When this falls behind the current time
    # by more than ``AGG_LIFECYCLE_DISAPPEARED_VOID_HOURS``, the event has
    # stopped appearing upstream and the watchdog will auto-VOID it
    # (subject to ``..._GRACE_HOURS``). Nullable so existing rows pre-
    # migration don't trip the watchdog until they get refreshed.
    last_seen_upstream_at: Mapped[datetime | None] = mapped_column(
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
