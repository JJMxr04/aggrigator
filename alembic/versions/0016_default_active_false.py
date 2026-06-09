"""Flip the DB-side default for ``core_sport.active`` and ``core_league.active``
back to ``false`` — both sports and leagues now default to inactive, matching
the model defaults. New rows are off until an operator explicitly enables them
in SQLAdmin (and a league still needs its parent Sport active to be walked).

This reverses 0006 (which had set both defaults to ``true``). Existing rows are
NOT touched by ``ALTER COLUMN ... SET DEFAULT`` — that only affects future
INSERTs that omit the column.

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "core_sport", "active", server_default=sa.false(),
        existing_type=sa.Boolean(), existing_nullable=False,
    )
    op.alter_column(
        "core_league", "active", server_default=sa.false(),
        existing_type=sa.Boolean(), existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "core_sport", "active", server_default=sa.true(),
        existing_type=sa.Boolean(), existing_nullable=False,
    )
    op.alter_column(
        "core_league", "active", server_default=sa.true(),
        existing_type=sa.Boolean(), existing_nullable=False,
    )
