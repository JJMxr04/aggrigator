"""Flip the DB-side default for ``core_sport.active`` and ``core_league.active``
from ``false`` to ``true`` — matches the model default + the seed-resets-active
behavior from plan §2.4.

Existing rows are NOT touched by ``ALTER COLUMN ... SET DEFAULT`` — that only
affects future INSERTs that omit the column. To flip existing data, use the
companion ops-console "Run seed" / "Run full_refresh" path (which now always
sets ``active=True``), or run the one-liner in the deploy notes.

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "core_sport", "active", server_default=sa.true(),
        existing_type=sa.Boolean(), existing_nullable=False,
    )
    op.alter_column(
        "core_league", "active", server_default=sa.true(),
        existing_type=sa.Boolean(), existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "core_sport", "active", server_default=sa.false(),
        existing_type=sa.Boolean(), existing_nullable=False,
    )
    op.alter_column(
        "core_league", "active", server_default=sa.false(),
        existing_type=sa.Boolean(), existing_nullable=False,
    )
