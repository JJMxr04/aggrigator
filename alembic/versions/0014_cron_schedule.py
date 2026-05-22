"""``cron_schedule`` — per-cron enabled flag for the ARQ scheduler.

Lets operators pause / resume timed crons from /ops/crons without
restarting the worker. ``enabled=False`` makes the scheduled tick a
no-op; manual ``Run now`` still works.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cron_schedule",
        sa.Column("cron_name", sa.String(64), primary_key=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_by_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("auth_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("cron_schedule")
