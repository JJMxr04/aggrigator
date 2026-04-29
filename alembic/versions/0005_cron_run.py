"""``cron_run`` table — execution log for scheduled and manual cron runs.

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cron_run",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cron_name", sa.String(64), nullable=False),
        sa.Column("trigger_source", sa.String(16), nullable=False),
        sa.Column(
            "started_by_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("auth_user.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("arq_job_id", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("items_processed", sa.Integer(), nullable=True),
        sa.Column("summary", postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        "ix_cron_run_cron_name_started_at", "cron_run",
        ["cron_name", "started_at"],
    )
    op.create_index(
        "ix_cron_run_running", "cron_run", ["cron_name"],
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "ix_cron_run_arq_job_id", "cron_run", ["arq_job_id"], unique=True,
    )


def downgrade() -> None:
    op.drop_table("cron_run")
