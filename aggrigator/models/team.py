"""Team — mirrors MDProject ``core_team``.

Composite-PK-by-string: ``id = "{league_id}:{team_id}"``. Same scheme MDProject
uses so payloads round-trip exactly.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from aggrigator.models.base import Base, TimestampMixin


class Team(Base, TimestampMixin):
    __tablename__ = "core_team"
    __table_args__ = (
        UniqueConstraint("league_id", "team_id", name="uq_core_team_league_id_team_id"),
        Index("ix_core_team_league_id_team_id", "league_id", "team_id"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), default=uuid.uuid4, unique=True, index=True, nullable=False
    )

    league_id: Mapped[str] = mapped_column(
        String(48), ForeignKey("core_league.id", ondelete="RESTRICT"), nullable=False
    )
    team_id: Mapped[str] = mapped_column(String(48), nullable=False)
    sport_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("core_sport.id", ondelete="RESTRICT"),
        nullable=True,
    )

    name_long: Mapped[str] = mapped_column(String(128), nullable=False)
    name_medium: Mapped[str] = mapped_column(String(64), default="", server_default="")
    name_short: Mapped[str] = mapped_column(String(32), default="", server_default="")

    primary_color: Mapped[str | None] = mapped_column(String(9), nullable=True)
    secondary_color: Mapped[str | None] = mapped_column(String(9), nullable=True)
    primary_contrast: Mapped[str | None] = mapped_column(String(9), nullable=True)
    secondary_contrast: Mapped[str | None] = mapped_column(String(9), nullable=True)

    stat_entity_id: Mapped[str] = mapped_column(String(8), default="", server_default="")

    # Aggregator divergence: store URL string instead of MDProject's ImageField.
    # The aggregator does not own asset hosting.
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    @staticmethod
    def synth_pk(league_id: str, team_id: str) -> str:
        return f"{league_id}:{team_id}"
