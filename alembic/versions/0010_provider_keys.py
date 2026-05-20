"""Cross-provider keys + Event.provider discriminator.

Adds the columns the registry loader (`aggrigator.ingest.registry`) writes
into so the aggregator can hold canonical sport/league/team identities
across two providers (odds-api.io for live, the-odds-api.com for
historical). See plans/analytics/historical_odds_plan/01-phase1-build.md
for the full design.

- ``core_sport``  / ``core_league``: add ``odds_api_io_key`` +
  ``the_odds_api_com_key`` so each row carries the provider-specific
  identifier alongside our canonical ``id``.
- ``core_league``: add ``can_pull_historical_odds`` — derived flag, set
  by the registry loader, gates historical-ingest at the orchestrator.
- ``core_team``: add ``canonical_name`` (NOT NULL after backfill),
  ``odds_api_io_key`` (backfilled from ``team_id``),
  ``the_odds_api_com_key``, ``match_confirmed``, ``match_source``.
- ``core_event_event``: add ``provider`` discriminator. Existing rows
  came from odds-api.io exclusively so they get backfilled to that.

Uses ``IF NOT EXISTS`` everywhere — the dev DB this lands on has some
of these columns already (``core_event_event.provider`` was applied
out-of-band before this migration was authored), so this revision must
no-op cleanly on partial state.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-19
"""
from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- core_sport --------------------------------------------------------
    op.execute("ALTER TABLE core_sport ADD COLUMN IF NOT EXISTS odds_api_io_key VARCHAR(64)")
    op.execute("ALTER TABLE core_sport ADD COLUMN IF NOT EXISTS the_odds_api_com_key VARCHAR(64)")

    # ---- core_league ------------------------------------------------------
    op.execute("ALTER TABLE core_league ADD COLUMN IF NOT EXISTS odds_api_io_key VARCHAR(64)")
    op.execute("ALTER TABLE core_league ADD COLUMN IF NOT EXISTS the_odds_api_com_key VARCHAR(64)")
    op.execute(
        "ALTER TABLE core_league ADD COLUMN IF NOT EXISTS "
        "can_pull_historical_odds BOOLEAN NOT NULL DEFAULT FALSE"
    )

    # ---- core_team --------------------------------------------------------
    # canonical_name is added nullable, backfilled from name_long, then
    # flipped to NOT NULL. Same dance for odds_api_io_key (backfilled
    # from the existing team_id since every pre-migration row came from
    # odds-api.io).
    op.execute("ALTER TABLE core_team ADD COLUMN IF NOT EXISTS canonical_name VARCHAR(128)")
    op.execute("ALTER TABLE core_team ADD COLUMN IF NOT EXISTS odds_api_io_key VARCHAR(48)")
    op.execute("ALTER TABLE core_team ADD COLUMN IF NOT EXISTS the_odds_api_com_key VARCHAR(128)")
    op.execute(
        "ALTER TABLE core_team ADD COLUMN IF NOT EXISTS "
        "match_confirmed BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute("ALTER TABLE core_team ADD COLUMN IF NOT EXISTS match_source VARCHAR(32)")

    op.execute("UPDATE core_team SET canonical_name = name_long WHERE canonical_name IS NULL")
    op.execute("UPDATE core_team SET odds_api_io_key = team_id WHERE odds_api_io_key IS NULL")
    # SET NOT NULL is idempotent on Postgres — safe to re-run.
    op.execute("ALTER TABLE core_team ALTER COLUMN canonical_name SET NOT NULL")

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_core_team_league_the_odds_api_com_key "
        "ON core_team (league_id, the_odds_api_com_key)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_core_team_league_canonical_name "
        "ON core_team (league_id, canonical_name)"
    )

    # ---- core_event_event -------------------------------------------------
    # ``provider`` may already exist from an out-of-band ALTER TABLE on the
    # dev DB that pre-dated this revision. IF NOT EXISTS keeps us idempotent.
    op.execute(
        "ALTER TABLE core_event_event ADD COLUMN IF NOT EXISTS "
        "provider VARCHAR(32) NOT NULL DEFAULT 'odds_api_io'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_core_event_event_provider_league "
        "ON core_event_event (provider, league_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_core_event_event_provider_league")
    op.execute("ALTER TABLE core_event_event DROP COLUMN IF EXISTS provider")

    op.execute("DROP INDEX IF EXISTS ix_core_team_league_canonical_name")
    op.execute("DROP INDEX IF EXISTS ix_core_team_league_the_odds_api_com_key")
    op.execute("ALTER TABLE core_team DROP COLUMN IF EXISTS match_source")
    op.execute("ALTER TABLE core_team DROP COLUMN IF EXISTS match_confirmed")
    op.execute("ALTER TABLE core_team DROP COLUMN IF EXISTS the_odds_api_com_key")
    op.execute("ALTER TABLE core_team DROP COLUMN IF EXISTS odds_api_io_key")
    op.execute("ALTER TABLE core_team DROP COLUMN IF EXISTS canonical_name")

    op.execute("ALTER TABLE core_league DROP COLUMN IF EXISTS can_pull_historical_odds")
    op.execute("ALTER TABLE core_league DROP COLUMN IF EXISTS the_odds_api_com_key")
    op.execute("ALTER TABLE core_league DROP COLUMN IF EXISTS odds_api_io_key")

    op.execute("ALTER TABLE core_sport DROP COLUMN IF EXISTS the_odds_api_com_key")
    op.execute("ALTER TABLE core_sport DROP COLUMN IF EXISTS odds_api_io_key")
