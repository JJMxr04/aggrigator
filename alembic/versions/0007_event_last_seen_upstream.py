"""Add ``Event.last_seen_upstream_at`` for the disappearance watchdog.

Used by the odds-api.io migration to detect events that vanish from the
upstream feed — the only signal odds-api.io gives for cancellations
(verified against the OpenAPI spec; no ``cancelled`` / ``postponed``
status values exist). See aggrigator-plan/odds-api/cancelled-suspended.md
§3 Layer 2 + §9 for the full design.

The column is nullable so existing rows pre-migration don't trip the
watchdog until the next ingest cycle refreshes them. We also backfill
non-finalized rows to ``now()`` so the disappearance clock starts from
the migration moment rather than NULL (which the watchdog skips).

Index is partial (WHERE is_finalized = false) so the watchdog scan stays
cheap as the table grows — only pending events need to be scanned.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "core_event_event",
        sa.Column(
            "last_seen_upstream_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_core_event_event_last_seen_upstream_pending",
        "core_event_event",
        ["last_seen_upstream_at"],
        postgresql_where=sa.text("is_finalized = false"),
    )
    # Backfill non-finalized rows so the disappearance clock starts now
    # rather than NULL. The watchdog skips NULL rows entirely (treated as
    # "never seen", which would auto-void everything pre-existing on the
    # first run). Finalized rows stay NULL — they're not scanned anyway.
    op.execute(
        "UPDATE core_event_event SET last_seen_upstream_at = now() "
        "WHERE is_finalized = false"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_core_event_event_last_seen_upstream_pending",
        table_name="core_event_event",
    )
    op.drop_column("core_event_event", "last_seen_upstream_at")
