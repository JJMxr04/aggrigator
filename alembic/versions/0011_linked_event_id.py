"""Add ``Event.linked_event_id`` so a the-odds-api.com row can FK back
to its odds-api.io counterpart.

Phase 2 redesign: each game ends up with two Event rows — the live one
(provider='odds_api_io') carries scores + lifecycle, and a paired
historical one (provider='the_odds_api_com') carries closing-line
markets. The toa row points at the live row via ``linked_event_id``
so analytics joins are a single FK hop. NULL on standalone rows.

ON DELETE SET NULL: if the live row goes away (vacuum, manual delete)
we don't want to cascade-kill the historical markets we paid for. The
linkage just becomes orphan.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-19
"""
from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE core_event_event ADD COLUMN IF NOT EXISTS "
        "linked_event_id VARCHAR(64) NULL"
    )
    # Self-FK. Named for clarity in ``\d core_event_event``.
    op.execute(
        "ALTER TABLE core_event_event "
        "ADD CONSTRAINT fk_core_event_event_linked_event_id "
        "FOREIGN KEY (linked_event_id) REFERENCES core_event_event(id) "
        "ON DELETE SET NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_core_event_event_linked_event_id "
        "ON core_event_event (linked_event_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_core_event_event_linked_event_id")
    op.execute(
        "ALTER TABLE core_event_event DROP CONSTRAINT IF EXISTS "
        "fk_core_event_event_linked_event_id"
    )
    op.execute("ALTER TABLE core_event_event DROP COLUMN IF EXISTS linked_event_id")
