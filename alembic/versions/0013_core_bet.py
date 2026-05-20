"""Create ``core_bet`` — user-tracked wagers for the bet-tracking page.

Phase D of the dashboard redesign. See
``plans/analytics/dashboard_and_data/07-bet-tracking.md``.

Tenant-scoped via FK to ``tenant_user``. ``event_id`` is a nullable FK
to ``core_event_event`` (SET NULL on delete) so bets on markets the
aggrigator doesn't track (props, parlays) can still be logged with
just a free-text label.

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "core_bet",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "event_id",
            sa.String(64),
            sa.ForeignKey("core_event_event.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("selection_type", sa.String(32), nullable=False),
        sa.Column("bookmaker", sa.String(64), nullable=True),
        sa.Column("stake", sa.Numeric(12, 2), nullable=False),
        sa.Column("decimal_odds", sa.Numeric(7, 3), nullable=False),
        sa.Column(
            "settlement_status",
            sa.String(16),
            nullable=False,
            server_default="open",
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payout", sa.Numeric(12, 2), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Primary list query — chronological order per tenant.
    op.create_index(
        "ix_core_bet_tenant_user_placed_at",
        "core_bet",
        ["tenant_user_id", "placed_at"],
    )
    # Filter open vs settled. Composite so the planner can use both
    # columns when the page filters by status.
    op.create_index(
        "ix_core_bet_tenant_user_settlement_status",
        "core_bet",
        ["tenant_user_id", "settlement_status"],
    )
    # Auto-settle hook reads ``WHERE event_id = X AND settlement_status = 'open'``.
    op.create_index(
        "ix_core_bet_event_id_settlement_status",
        "core_bet",
        ["event_id", "settlement_status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_core_bet_event_id_settlement_status", table_name="core_bet",
    )
    op.drop_index(
        "ix_core_bet_tenant_user_settlement_status", table_name="core_bet",
    )
    op.drop_index(
        "ix_core_bet_tenant_user_placed_at", table_name="core_bet",
    )
    op.drop_table("core_bet")
