"""Rename ``the_odds_api_com_key`` / ``can_pull_historical_odds`` columns
to their TheSportsDB equivalents.

The project pivoted from the-odds-api.com (historical odds) to
TheSportsDB (historical scores). See
``plans/analytics/TheSportsDB/02-phase2-models-and-migration.md``.

Renames in place (no data loss) — the the_odds_api_com_key values are
about to be overwritten by ``load_registry`` against the rewritten
``aggrigator/data/sports/*.json`` files (Phase 1), but the column
metadata + index history stays intact.

- ``core_sport.the_odds_api_com_key``   → ``thesportsdb_id``
- ``core_league.the_odds_api_com_key``  → ``thesportsdb_id``
- ``core_league.can_pull_historical_odds`` → ``can_pull_historical_scores``
- ``core_team.the_odds_api_com_key``    → ``thesportsdb_team_id``
- index ``ix_core_team_league_the_odds_api_com_key``
        → ``ix_core_team_league_thesportsdb_team_id``

Idempotent — wrapped in ``information_schema`` existence checks so a
re-run on partially-migrated state is safe.

``core_event_event.provider`` keeps its default ``"odds_api_io"`` and
its index unchanged. Historical rows written by Phase 3 carry
``provider='thesportsdb'`` — no enum constraint is added.

``core_event_event.linked_event_id`` is kept (orphaned). No current
consumer; reserved for a future capture-from-live odds pipeline.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-19
"""
from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _rename_column_if_exists(table: str, old_name: str, new_name: str) -> None:
    """Idempotent column rename. Skips if old column already gone OR new
    column already exists (in case the rename half-completed)."""
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_name = '{table}' AND column_name = '{old_name}'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_name = '{table}' AND column_name = '{new_name}'
            ) THEN
                ALTER TABLE {table} RENAME COLUMN {old_name} TO {new_name};
            END IF;
        END $$;
        """
    )


def _rename_index_if_exists(old_name: str, new_name: str) -> None:
    """Idempotent index rename."""
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_indexes
                 WHERE indexname = '{old_name}'
            ) AND NOT EXISTS (
                SELECT 1 FROM pg_indexes
                 WHERE indexname = '{new_name}'
            ) THEN
                ALTER INDEX {old_name} RENAME TO {new_name};
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    _rename_column_if_exists("core_sport", "the_odds_api_com_key", "thesportsdb_id")
    _rename_column_if_exists("core_league", "the_odds_api_com_key", "thesportsdb_id")
    _rename_column_if_exists(
        "core_league", "can_pull_historical_odds", "can_pull_historical_scores"
    )
    _rename_column_if_exists("core_team", "the_odds_api_com_key", "thesportsdb_team_id")
    _rename_index_if_exists(
        "ix_core_team_league_the_odds_api_com_key",
        "ix_core_team_league_thesportsdb_team_id",
    )


def downgrade() -> None:
    _rename_index_if_exists(
        "ix_core_team_league_thesportsdb_team_id",
        "ix_core_team_league_the_odds_api_com_key",
    )
    _rename_column_if_exists("core_team", "thesportsdb_team_id", "the_odds_api_com_key")
    _rename_column_if_exists(
        "core_league", "can_pull_historical_scores", "can_pull_historical_odds"
    )
    _rename_column_if_exists("core_league", "thesportsdb_id", "the_odds_api_com_key")
    _rename_column_if_exists("core_sport", "thesportsdb_id", "the_odds_api_com_key")
