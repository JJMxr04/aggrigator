"""Populate random team colors for local dev (aggregator side).

Mirror of MDProject's ``randomize_team_colors`` management command. The
aggregator's /v1/events TeamOut serialises all four color fields, so
randomising ``Team.primary_color`` (etc.) here surfaces the tints in the
upcoming-detail + pick-modal surfaces that read the aggregator catalog.

Dev-only data; no schema change. Idempotent by default (fills only NULL
primary_color); pass --overwrite to re-randomise every team.

    venv/bin/python scripts/randomize_team_colors.py
    venv/bin/python scripts/randomize_team_colors.py --overwrite
"""

from __future__ import annotations

import asyncio
import random
import sys

from sqlalchemy import select

from aggrigator.db import session_scope
from aggrigator.models.team import Team

_COLOR_FIELDS = ("primary_color", "secondary_color", "primary_contrast", "secondary_contrast")


def _rand_hex() -> str:
    return "#%06x" % random.randint(0, 0xFFFFFF)  # nosec B311 -- cosmetic team colors, not security


async def main(overwrite: bool) -> None:
    async with session_scope() as session:
        stmt = select(Team)
        if not overwrite:
            stmt = stmt.where(Team.primary_color.is_(None))
        teams = (await session.scalars(stmt)).all()
        for team in teams:
            for fld in _COLOR_FIELDS:
                setattr(team, fld, _rand_hex())
        scope = "all" if overwrite else "null-color"
        print(f"Randomised colors on {len(teams)} {scope} team(s).")


if __name__ == "__main__":
    overwrite = "--overwrite" in sys.argv[1:]
    asyncio.run(main(overwrite))
