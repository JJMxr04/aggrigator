"""``core_team_logo`` — odds-api crest bytes cached in Postgres.

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "core_team_logo",
        sa.Column("team_id", sa.String(80), primary_key=True),
        sa.Column("image", sa.LargeBinary(), nullable=True),
        sa.Column("content_type", sa.String(64), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("etag", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source", sa.String(32), nullable=False, server_default="odds-api"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["team_id"], ["core_team.id"],
            name="fk_core_team_logo_team_id_core_team",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("core_team_logo")
