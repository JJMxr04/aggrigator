"""Webhook subscription + delivery tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_endpoint",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("auth_user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret_ciphertext", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("events", postgresql.ARRAY(sa.Text()),
                  nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(),
                  nullable=False, server_default=sa.true()),
        sa.Column("scope", sa.String(16),
                  nullable=False, server_default="full"),
        sa.Column("created", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_webhook_endpoint_user_id_enabled", "webhook_endpoint",
        ["user_id", "enabled"],
    )

    op.create_table(
        "webhook_delivery",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("webhook_endpoint.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("event_id", sa.String(64),
                  sa.ForeignKey("core_event_event.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("event_name", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "endpoint_id", "idempotency_key",
            name="uq_webhook_delivery_endpoint_id_idempotency_key",
        ),
    )
    op.create_index(
        "ix_webhook_delivery_due", "webhook_delivery",
        ["next_retry_at"],
        postgresql_where=sa.text("delivered_at IS NULL"),
    )
    op.create_index(
        "ix_webhook_delivery_event_id", "webhook_delivery", ["event_id"],
    )


def downgrade() -> None:
    op.drop_table("webhook_delivery")
    op.drop_table("webhook_endpoint")
