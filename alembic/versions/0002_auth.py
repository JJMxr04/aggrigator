"""Auth + ClientApp tables.

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CITEXT is a Postgres extension. Required for case-insensitive User.email.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "auth_user",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", postgresql.CITEXT(), unique=True, nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="user"),
        sa.Column("tier", sa.String(16), nullable=False, server_default="free"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
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
        "auth_refresh_token",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("auth_user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.Text(), unique=True, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
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


def downgrade() -> None:
    op.drop_table("client_app")
    op.drop_table("auth_refresh_token")
    op.drop_table("auth_api_key")
    op.drop_table("auth_user")
    # Don't DROP EXTENSION citext — it might be in use elsewhere.
