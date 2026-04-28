"""Initial schema — domain tables (Sport, League, Team, Event, Bookmaker,
BookmakerSelection, Market, Selection, OddsQuote).

Auth / webhook / audit tables land in 0002 once Phase 1 starts. Keeping them
out of 0001 lets the parity-port land first without dragging the auth layer
along.

Revision ID: 0001
Revises:
Create Date: 2026-04-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "core_sport",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("short_name", sa.String(48), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "core_league",
        sa.Column("id", sa.String(48), primary_key=True),
        sa.Column("sport_id", sa.String(32),
                  sa.ForeignKey("core_sport.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("short_name", sa.String(48), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("refresh_cadence_minutes", sa.Integer(), nullable=False, server_default="720"),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_core_league_sport_id_active", "core_league", ["sport_id", "active"]
    )

    op.create_table(
        "core_team",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("public_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  unique=True, nullable=False),
        sa.Column("league_id", sa.String(48),
                  sa.ForeignKey("core_league.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("team_id", sa.String(48), nullable=False),
        sa.Column("sport_id", sa.String(32),
                  sa.ForeignKey("core_sport.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("name_long", sa.String(128), nullable=False),
        sa.Column("name_medium", sa.String(64), nullable=False, server_default=""),
        sa.Column("name_short", sa.String(32), nullable=False, server_default=""),
        sa.Column("primary_color", sa.String(9), nullable=True),
        sa.Column("secondary_color", sa.String(9), nullable=True),
        sa.Column("primary_contrast", sa.String(9), nullable=True),
        sa.Column("secondary_contrast", sa.String(9), nullable=True),
        sa.Column("stat_entity_id", sa.String(8), nullable=False, server_default=""),
        sa.Column("logo_url", sa.Text(), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("league_id", "team_id", name="uq_core_team_league_id_team_id"),
    )
    op.create_index("ix_core_team_public_id", "core_team", ["public_id"])
    op.create_index("ix_core_team_league_id_team_id", "core_team", ["league_id", "team_id"])

    op.create_table(
        "core_event_event",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("public_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  unique=True, nullable=False),
        sa.Column("sport_id", sa.String(32),
                  sa.ForeignKey("core_sport.id", ondelete="SET NULL"), nullable=True),
        sa.Column("league_id", sa.String(48),
                  sa.ForeignKey("core_league.id", ondelete="SET NULL"), nullable=True),
        sa.Column("type", sa.String(16), nullable=False, server_default=""),
        sa.Column("season_label", sa.String(64), nullable=False, server_default=""),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_type", sa.String(32), nullable=False, server_default=""),
        sa.Column("status_display", sa.String(64), nullable=False, server_default=""),
        sa.Column("current_period_id", sa.String(16), nullable=False, server_default=""),
        sa.Column("is_live", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_finalized", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("completed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("home_team_id", sa.String(80),
                  sa.ForeignKey("core_team.id", ondelete="SET NULL"), nullable=True),
        sa.Column("away_team_id", sa.String(80),
                  sa.ForeignKey("core_team.id", ondelete="SET NULL"), nullable=True),
        sa.Column("home_score", sa.Integer(), nullable=True),
        sa.Column("away_score", sa.Integer(), nullable=True),
        sa.Column("winner_code", sa.SmallInteger(), nullable=True),
        sa.Column("winner", sa.String(255), nullable=True),
        sa.Column("feed_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_provider_refresh_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_webhook_sent_hash", sa.String(64), nullable=True),
        sa.Column("last_webhook_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_core_event_event_public_id", "core_event_event", ["public_id"])
    op.create_index(
        "ix_core_event_event_league_status_start", "core_event_event",
        ["league_id", "status_type", "start_time"],
    )
    op.create_index(
        "ix_core_event_event_sport_status_start", "core_event_event",
        ["sport_id", "status_type", "start_time"],
    )
    op.create_index("ix_core_event_event_start_time", "core_event_event", ["start_time"])
    op.create_index("ix_core_event_event_status_type", "core_event_event", ["status_type"])
    op.create_index("ix_core_event_event_is_live", "core_event_event", ["is_live"])

    op.create_table(
        "core_bookmaker",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "core_market",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("event_id", sa.String(64),
                  sa.ForeignKey("core_event_event.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sport_id", sa.String(32),
                  sa.ForeignKey("core_sport.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False, server_default="FULL_GAME"),
        sa.Column("line", sa.Numeric(6, 2), nullable=True),
        sa.Column("side", sa.String(8), nullable=False, server_default=""),
        sa.Column("provider", sa.String(32), nullable=False, server_default="sportsgameodds"),
        sa.Column("provider_market_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("provider_choice_group", sa.String(16), nullable=False, server_default=""),
        sa.Column("subject_team_id", sa.String(80),
                  sa.ForeignKey("core_team.id", ondelete="SET NULL"), nullable=True),
        sa.Column("is_live", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("suspended", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_updated", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_core_market_category", "core_market", ["category"])
    op.create_index("ix_core_market_type", "core_market", ["type"])
    op.create_index("ix_core_market_provider_market_id", "core_market", ["provider_market_id"])
    op.create_index("ix_core_market_last_updated", "core_market", ["last_updated"])
    op.create_index(
        "ix_core_market_event_id_category_scope", "core_market",
        ["event_id", "category", "scope"],
    )
    op.create_index(
        "ix_core_market_sport_id_category_is_live", "core_market",
        ["sport_id", "category", "is_live"],
    )
    op.create_index("ix_core_market_event_id_type", "core_market", ["event_id", "type"])

    op.create_table(
        "core_selection",
        sa.Column("id", sa.String(160), primary_key=True),
        sa.Column("market_id", sa.String(128),
                  sa.ForeignKey("core_market.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("suspended", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("decimal_odds", sa.Numeric(8, 4), nullable=True),
        sa.Column("opening_decimal_odds", sa.Numeric(8, 4), nullable=True),
        sa.Column("movement", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("settlement_status", sa.String(8), nullable=False, server_default="PENDING"),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settlement_source", sa.String(16), nullable=False, server_default=""),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_core_selection_settlement_status", "core_selection", ["settlement_status"])
    op.create_index(
        "ix_core_selection_market_id_type", "core_selection", ["market_id", "type"]
    )
    op.create_index(
        "ix_core_selection_settlement_status_market_id", "core_selection",
        ["settlement_status", "market_id"],
    )

    op.create_table(
        "core_bookmaker_selection",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("selection_id", sa.String(160),
                  sa.ForeignKey("core_selection.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bookmaker_id", sa.String(32),
                  sa.ForeignKey("core_bookmaker.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("decimal_odds", sa.Numeric(8, 4), nullable=True),
        sa.Column("spread", sa.Numeric(6, 2), nullable=True),
        sa.Column("over_under", sa.Numeric(6, 2), nullable=True),
        sa.Column("available", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("deeplink", sa.String(500), nullable=False, server_default=""),
        sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "selection_id", "bookmaker_id",
            name="uq_core_bookmaker_selection_selection_id_bookmaker_id",
        ),
    )
    op.create_index(
        "ix_core_bookmaker_selection_selection_id_available",
        "core_bookmaker_selection", ["selection_id", "available"],
    )
    op.create_index(
        "ix_core_bookmaker_selection_bookmaker_id_available",
        "core_bookmaker_selection", ["bookmaker_id", "available"],
    )

    op.create_table(
        "core_odds_quote",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("selection_id", sa.String(160),
                  sa.ForeignKey("core_selection.id", ondelete="CASCADE"), nullable=False),
        sa.Column("decimal_odds", sa.Numeric(8, 4), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "selection_id", "captured_at",
            name="uq_core_odds_quote_selection_id_captured_at",
        ),
    )
    op.create_index("ix_core_odds_quote_captured_at", "core_odds_quote", ["captured_at"])
    op.create_index(
        "ix_core_odds_quote_selection_id_captured_at",
        "core_odds_quote", ["selection_id", "captured_at"],
    )


def downgrade() -> None:
    op.drop_table("core_odds_quote")
    op.drop_table("core_bookmaker_selection")
    op.drop_table("core_selection")
    op.drop_table("core_market")
    op.drop_table("core_bookmaker")
    op.drop_table("core_event_event")
    op.drop_table("core_team")
    op.drop_table("core_league")
    op.drop_table("core_sport")
