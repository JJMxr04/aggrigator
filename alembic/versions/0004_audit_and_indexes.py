"""Audit log table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_api_key_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("event_name", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("audit_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_audit_log_actor_user_id_created_at", "audit_log",
        ["actor_user_id", "created_at"],
    )
    op.create_index(
        "ix_audit_log_event_name_created_at", "audit_log",
        ["event_name", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("audit_log")
