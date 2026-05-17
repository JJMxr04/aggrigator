"""Tenant tables — TenantUser + TenantApiKey.

Mirror of MDProject users for the analytics-gate / Paradise channel. See
``subscription-plan/02-data-model.md`` and ``04-internal-api.md``.

CITEXT extension is already present (created in 0002_auth.py); we reuse
it for ``tenant_user.email`` so case-insensitive lookups match MDProject.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_user",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "external_user_id",
            postgresql.UUID(as_uuid=True),
            unique=True,
            nullable=False,
        ),
        sa.Column("email", postgresql.CITEXT(), unique=True, nullable=False),
        sa.Column("tier", sa.String(16), nullable=False, server_default="FREE"),
        sa.Column(
            "status", sa.String(24), nullable=False, server_default="active",
        ),
        sa.Column(
            "features",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
    # The unique constraint already creates a btree index on
    # external_user_id; no extra index needed for that column.

    op.create_table(
        "tenant_api_key",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Plaintext lookup key, format ``agg_{env}_{head5}`` from
        # aggrigator/security/api_keys.py (~13-14 chars but TEXT to avoid
        # future widening on env-name change).
        sa.Column("prefix", sa.Text(), unique=True, nullable=False),
        # argon2id hash of the secret tail. Module: api_keys._HASHER.
        sa.Column("key_hash", sa.Text(), nullable=False),
        # Last 4 chars of the raw key — display-only ("…fxxxx") so the
        # operator can confirm which key got revoked without seeing the
        # secret. Mirrors the existing api_keys.GeneratedKey shape.
        sa.Column("last_four", sa.String(4), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index(
        "ix_tenant_api_key_tenant_user_id",
        "tenant_api_key",
        ["tenant_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_tenant_api_key_tenant_user_id", table_name="tenant_api_key")
    op.drop_table("tenant_api_key")
    op.drop_table("tenant_user")
