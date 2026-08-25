"""Reference reads — sports, leagues, bookmakers, market types.

These are the dropdowns / filter-lookup calls the upload portal uses to
populate event-pick UI before any specific event is chosen. Cheap queries on
small tables. Keyed reads: nothing in the aggregator is
anonymous — enforcement is flag-staged via ``keyed_reads_gate``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response

from aggrigator.api._http_cache import cached_json
from aggrigator.deps import SessionDep, keyed_reads_gate
from aggrigator.queries.references import ReferenceQueries
from aggrigator.schemas.bookmaker import BookmakerOut
from aggrigator.schemas.league import LeagueOut
from aggrigator.schemas.sport import SportOut

# Router-level so a future endpoint can't be added keyless by accident.
router = APIRouter(
    prefix="/v1", tags=["reference"], dependencies=[Depends(keyed_reads_gate)],
)

# These tables change rarely (new sport added: maybe once a quarter; new
# league: similarly low frequency). A 10-minute TTL is conservative — the
# trade-off is at most 10 minutes between a new row appearing in DB and
# clients seeing it. Still revalidates via ETag on every request inside
# the window.
REFERENCE_MAX_AGE = 600

_queries = ReferenceQueries()


@router.get("/sports")
async def list_sports(
    session: SessionDep,
    response: Response,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    active: bool | None = Query(default=None),
):
    rows = await _queries.list_sports(session, active=active)
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
    rows = await _queries.list_leagues(session, sport_id=sport_id, active=active)
    payload = [LeagueOut.model_validate(r) for r in rows]
    return cached_json(payload, response, if_none_match, max_age=REFERENCE_MAX_AGE)


@router.get("/bookmakers")
async def list_bookmakers(
    session: SessionDep,
    response: Response,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    active: bool | None = Query(default=None),
):
    rows = await _queries.list_bookmakers(session, active=active)
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
    payload = await _queries.list_market_types(session, sport_id=sport_id)
    return cached_json(payload, response, if_none_match, max_age=REFERENCE_MAX_AGE)
