"""Bulk team catalog read for the MDProject team-data sync (design §3).

Keyed (router-level ``keyed_reads_gate``) exactly like ``api/events.py`` —
no team endpoint can be added keyless by accident. Trimmed ``TeamSyncOut``
projection: provider keys / match state / public_id / logo_url never leave
this service on this path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from aggrigator.deps import SessionDep, keyed_reads_gate
from aggrigator.models import Team
from aggrigator.schemas.team import TeamListOut, TeamSyncOut

router = APIRouter(
    prefix="/v1/teams", tags=["teams"], dependencies=[Depends(keyed_reads_gate)],
)


@router.get("")
async def list_teams(
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=200, ge=1, le=500),
    league_id: str | None = Query(default=None, description="leagueID, e.g. NFL"),
) -> TeamListOut:
    count_stmt = select(func.count()).select_from(Team)
    rows_stmt = select(Team).order_by(Team.id)
    if league_id:
        count_stmt = count_stmt.where(Team.league_id == league_id)
        rows_stmt = rows_stmt.where(Team.league_id == league_id)

    total = (await session.scalar(count_stmt)) or 0
    rows = list(
        await session.scalars(
            rows_stmt.limit(page_size).offset((page - 1) * page_size)
        )
    )
    pages = max(1, -(-total // page_size))
    return TeamListOut(
        items=[TeamSyncOut.model_validate(t) for t in rows],
        page=page,
        page_size=page_size,
        pages=pages,
        total=total,
    )
