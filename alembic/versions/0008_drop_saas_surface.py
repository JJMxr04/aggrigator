"""Drop the multi-tenant SaaS surface.

Aggrigator is now single-tenant (MDProject only). This migration tears down:

- ``auth_api_key`` — per-user API keys are gone (no key auth on data
  endpoints).
- ``webhook_endpoint`` — replaced by a single hardcoded receiver configured
  via ``AGG_WEBHOOK_TARGET_URL`` + ``AGG_WEBHOOK_SECRET`` env vars.
- ``webhook_delivery.endpoint_id`` — only one receiver, so the FK is
  redundant. Replaces the (endpoint_id, idempotency_key) unique constraint
  with a unique constraint on idempotency_key alone.
- ``client_app`` — drove rate-limit promotion via ``X-Client-App``. Rate
  limiting itself is gone, so the table goes too.
- ``auth_user.tier`` — fed the rate-limit ladder. Now meaningless.
- ``audit_log.actor_api_key_id`` — no API keys = no key-actor rows.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # webhook_delivery: drop the endpoint_id FK + composite unique constraint
    # BEFORE dropping the webhook_endpoint table that it references.
    op.drop_constraint(
        "uq_webhook_delivery_endpoint_id_idempotency_key",
        "webhook_delivery", type_="unique",
    )
    op.drop_column("webhook_delivery", "endpoint_id")
    op.create_unique_constraint(
        "uq_webhook_delivery_idempotency_key",
        "webhook_delivery", ["idempotency_key"],
    )

    op.drop_index(
        "ix_webhook_endpoint_user_id_enabled", table_name="webhook_endpoint"
    )
    op.drop_table("webhook_endpoint")

    op.drop_index(
        "ix_auth_api_key_user_id_revoked_at", table_name="auth_api_key"
    )
    op.drop_table("auth_api_key")

    op.drop_table("client_app")

    op.drop_column("auth_user", "tier")
    op.drop_column("audit_log", "actor_api_key_id")


def downgrade() -> None:
    # Best-effort restore of the SaaS surface — secrets in webhook_endpoint
    # and key_hash in auth_api_key cannot be recovered from this rollback;
    # downstream operators must re-issue keys + re-rotate webhook secrets.
    op.add_column(
        "audit_log",
        sa.Column("actor_api_key_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "auth_user",
        sa.Column(
            "tier", sa.String(16),
            nullable=False, server_default="free",
        ),
    )

    op.create_table(
        "client_app",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.Text(), unique=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), unique=True, nullable=False),
        sa.Column("client_secret_hash", sa.Text(), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False, server_default="free"),
        sa.Column("trusted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "auth_api_key",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("auth_user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), unique=True, nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("last_four", sa.String(4), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", postgresql.INET(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_auth_api_key_user_id_revoked_at", "auth_api_key",
        ["user_id", "revoked_at"],
    )

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

    op.drop_constraint(
        "uq_webhook_delivery_idempotency_key",
        "webhook_delivery", type_="unique",
    )
    op.add_column(
        "webhook_delivery",
        sa.Column(
            "endpoint_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("webhook_endpoint.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_unique_constraint(
        "uq_webhook_delivery_endpoint_id_idempotency_key",
        "webhook_delivery", ["endpoint_id", "idempotency_key"],
    )
