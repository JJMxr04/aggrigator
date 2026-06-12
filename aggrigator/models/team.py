"""Team — mirrors MDProject ``core_team``.

Composite-PK-by-string: ``id = "{league_id}:{team_id}"``. Same scheme MDProject
uses so payloads round-trip exactly. Roster-filler rows inserted by the
registry loader (teams with no provider key yet) use a synthesized
team_id of the shape ``_canon:<slug>``; see ``synth_filler_team_id``.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from aggrigator.models.base import Base, TimestampMixin


class Team(Base, TimestampMixin):
    __tablename__ = "core_team"
    __table_args__ = (
        UniqueConstraint("league_id", "team_id", name="uq_core_team_league_id_team_id"),
        Index("ix_core_team_league_id_team_id", "league_id", "team_id"),
        Index("ix_core_team_league_thesportsdb_team_id", "league_id", "thesportsdb_team_id"),
        Index("ix_core_team_league_canonical_name", "league_id", "canonical_name"),
        # Mirrors migration 0001 (plain index; the UNIQUE lives on the
        # column as a constraint) — index=True+unique=True would make
        # autogenerate flap between constraint and unique-index forever.
        Index("ix_core_team_public_id", "public_id"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), default=uuid.uuid4, unique=True, nullable=False
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

    # Canonical-name + cross-provider matching. Owned by the registry
    # loader (``aggrigator.ingest.registry``). ``canonical_name`` is the
    # authoritative team identity; ``odds_api_io_key`` /
    # ``thesportsdb_team_id`` store the per-provider strings used at
    # ingest-time lookup. Roster_filler rows have both keys NULL until
    # an operator fills them in or the live feed surfaces a match.
    canonical_name: Mapped[str] = mapped_column(String(128), nullable=False)
    odds_api_io_key: Mapped[str | None] = mapped_column(String(48), nullable=True)
    thesportsdb_team_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    match_confirmed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # "fuzzy_match" | "manual_override" | "roster_filler" — informational.
    match_source: Mapped[str | None] = mapped_column(String(32), nullable=True)

    @staticmethod
    def synth_pk(league_id: str, team_id: str) -> str:
        return f"{league_id}:{team_id}"

    @staticmethod
    def synth_filler_team_id(canonical_name: str) -> str:
        """Stable placeholder team_id when no provider key exists yet.

        Format: ``_canon:<slug>`` where slug is the canonical_name
        lowercased with non-alphanumeric runs collapsed to ``_``. The
        leading underscore guarantees no collision with real provider
        team ids (odds-api.io uses digits or short alpha codes).

        Once a real ``odds_api_io_key`` arrives via the live feed, the
        upsert path keeps the row's PK stable but populates
        ``odds_api_io_key`` in its own column — see plan 01 §5.
        """
        slug = re.sub(r"[^a-z0-9]+", "_", canonical_name.lower()).strip("_")
        return f"_canon:{slug}"
