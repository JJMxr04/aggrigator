"""Reference reads — sports, leagues, bookmakers, market types.

These are the dropdowns / filter-lookup calls the upload portal uses to
populate event-pick UI before any specific event is chosen. Cheap queries on
small tables; auth required so we can rate-limit by key.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from aggrigator.deps import SessionDep, require_user
from aggrigator.models import Bookmaker, League, Market, Sport, User
from aggrigator.schemas.bookmaker import BookmakerOut
from aggrigator.schemas.league import LeagueOut
from aggrigator.schemas.sport import SportOut

router = APIRouter(prefix="/v1", tags=["reference"])


@router.get("/sports", response_model=list[SportOut])
async def list_sports(
    session: SessionDep,
    _user: Annotated[User, Depends(require_user)],
    active: bool | None = Query(default=None),
) -> list[Sport]:
    stmt = select(Sport).order_by(Sport.name)
    if active is not None:
        stmt = stmt.where(Sport.active == active)
    return list(await session.scalars(stmt))


@router.get("/leagues", response_model=list[LeagueOut])
async def list_leagues(
    session: SessionDep,
    _user: Annotated[User, Depends(require_user)],
    sport_id: str | None = Query(default=None),
    active: bool | None = Query(default=None),
) -> list[League]:
    stmt = select(League).order_by(League.sport_id, League.name)
    if sport_id is not None:
        stmt = stmt.where(League.sport_id == sport_id)
    if active is not None:
        stmt = stmt.where(League.active == active)
    return list(await session.scalars(stmt))


@router.get("/bookmakers", response_model=list[BookmakerOut])
async def list_bookmakers(
    session: SessionDep,
    _user: Annotated[User, Depends(require_user)],
    active: bool | None = Query(default=None),
) -> list[Bookmaker]:
    stmt = select(Bookmaker).order_by(Bookmaker.name)
    if active is not None:
        stmt = stmt.where(Bookmaker.active == active)
    return list(await session.scalars(stmt))


@router.get("/market-types", response_model=list[str])
async def list_market_types(
    session: SessionDep,
    _user: Annotated[User, Depends(require_user)],
    sport_id: str | None = Query(default=None),
) -> list[str]:
    """Distinct values of ``Market.type`` currently in the DB.

    Used by the MDProject portal's "What's available" page to render the
    actual list of market names the operator can pick from, replacing the
    hand-maintained static list.

    Filter by ``sport_id`` to scope to one sport's markets. Cheap query —
    one ``SELECT DISTINCT`` over an indexed column.
    """
    stmt = select(Market.type).distinct().order_by(Market.type)
    if sport_id is not None:
        stmt = stmt.where(Market.sport_id == sport_id)
    rows = await session.scalars(stmt)
    return [t for t in rows if t]
