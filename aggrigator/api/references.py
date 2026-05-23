"""Reference reads — sports, leagues, bookmakers, market types.

These are the dropdowns / filter-lookup calls the upload portal uses to
populate event-pick UI before any specific event is chosen. Cheap queries on
small tables. Public — no auth on the aggrigator's data plane.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Response
from sqlalchemy import select

from aggrigator.api._http_cache import cached_json
from aggrigator.deps import SessionDep
from aggrigator.models import Bookmaker, League, Market, Sport
from aggrigator.schemas.bookmaker import BookmakerOut
from aggrigator.schemas.league import LeagueOut
from aggrigator.schemas.sport import SportOut

router = APIRouter(prefix="/v1", tags=["reference"])

# These tables change rarely (new sport added: maybe once a quarter; new
# league: similarly low frequency). A 10-minute TTL is conservative — the
# trade-off is at most 10 minutes between a new row appearing in DB and
# clients seeing it. Still revalidates via ETag on every request inside
# the window.
REFERENCE_MAX_AGE = 600


@router.get("/sports")
async def list_sports(
    session: SessionDep,
    response: Response,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    active: bool | None = Query(default=None),
):
    stmt = select(Sport).order_by(Sport.name)
    if active is not None:
        stmt = stmt.where(Sport.active == active)
    rows = list(await session.scalars(stmt))
    payload = [SportOut.model_validate(r) for r in rows]
    return cached_json(payload, response, if_none_match, max_age=REFERENCE_MAX_AGE)


@router.get("/leagues")
async def list_leagues(
    session: SessionDep,
    response: Response,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    sport_id: str | None = Query(default=None),
    active: bool | None = Query(default=None),
):
    stmt = select(League).order_by(League.sport_id, League.name)
    if sport_id is not None:
        stmt = stmt.where(League.sport_id == sport_id)
    if active is not None:
        stmt = stmt.where(League.active == active)
    rows = list(await session.scalars(stmt))
    payload = [LeagueOut.model_validate(r) for r in rows]
    return cached_json(payload, response, if_none_match, max_age=REFERENCE_MAX_AGE)


@router.get("/bookmakers")
async def list_bookmakers(
    session: SessionDep,
    response: Response,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    active: bool | None = Query(default=None),
):
    stmt = select(Bookmaker).order_by(Bookmaker.name)
    if active is not None:
        stmt = stmt.where(Bookmaker.active == active)
    rows = list(await session.scalars(stmt))
    payload = [BookmakerOut.model_validate(r) for r in rows]
    return cached_json(payload, response, if_none_match, max_age=REFERENCE_MAX_AGE)


@router.get("/market-types")
async def list_market_types(
    session: SessionDep,
    response: Response,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    sport_id: str | None = Query(default=None),
):
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
    payload = [t for t in rows if t]
    return cached_json(payload, response, if_none_match, max_age=REFERENCE_MAX_AGE)
