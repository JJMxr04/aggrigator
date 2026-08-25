"""Event reads — list, detail, markets-for-event.

Same query contract as MDProject ``core/event/views/api/events.py`` and
``markets.py``. The aggregator does **not** trigger inline provider
calls on read (unlike MDProject's ``get_event_odds``); a future on-demand
refresh job is enqueued instead.
"""

from __future__ import annotations

from datetime import date as Date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status

from aggrigator.api._http_cache import cached_json
from aggrigator.config import get_settings
from aggrigator.deps import SessionDep, keyed_reads_gate
from aggrigator.ingest.lifecycle import compute_stale
from aggrigator.models import Event, Market
from aggrigator.queries.events import EventQueries
from aggrigator.queries.filters import EventListFilters, MarketFilters
from aggrigator.schemas.event import (
    EventDetailOut,
    EventOut,
    EventWithMarketsOut,
    OddsMeta,
)
from aggrigator.schemas.market import MarketOut
from aggrigator.schemas.pagination import Page, PageParams
from aggrigator.schemas.team import team_logo_url

# Keyed reads — router-level so a future endpoint can't
# be added keyless by accident; enforcement flag-staged via keyed_reads_gate.
router = APIRouter(
    prefix="/v1/events", tags=["events"], dependencies=[Depends(keyed_reads_gate)],
)

# Short TTLs — odds change frequently; we cache only long enough to
# absorb thundering-herd page renders and let HTTP intermediaries collapse
# repeated GETs while a single browser/portal session keeps moving.
LIST_MAX_AGE = 30      # seconds — list of events (markets move fast)
DETAIL_MAX_AGE = 30    # seconds — single event w/ odds
MARKETS_MAX_AGE = 30   # seconds — markets list


_queries = EventQueries()


def _event_out(event: Event, public_base: str) -> EventOut:
    """Return EventOut for event with team logo_url overridden to the aggregator's keyless endpoint."""
    out = EventOut.model_validate(event)
    if out.home_team is not None:
        out.home_team.logo_url = team_logo_url(out.home_team.id, public_base=public_base)
    if out.away_team is not None:
        out.away_team.logo_url = team_logo_url(out.away_team.id, public_base=public_base)
    return out


def _market_out(market: Market, public_base: str) -> MarketOut:
    out = MarketOut.model_validate(market)
    if out.subject_team is not None:
        out.subject_team.logo_url = team_logo_url(
            out.subject_team.id, public_base=public_base
        )
    return out


@router.get("")
async def list_events(
    session: SessionDep,
    response: Response,
    page_params: Annotated[PageParams, Depends()],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    sport: str | None = Query(default=None, description="sportID, e.g. FOOTBALL"),
    league: str | None = Query(default=None, description="leagueID, e.g. NFL"),
    live: bool | None = Query(default=None),
    date: Date | None = Query(default=None, description="YYYY-MM-DD"),
    starts_after: datetime | None = Query(
        default=None,
        description="ISO8601 datetime; inclusive lower bound on start_time. "
                    "Overrides the default 3h-ago window. Ignored if `date` is set.",
    ),
    starts_before: datetime | None = Query(
        default=None,
        description="ISO8601 datetime; exclusive upper bound on start_time. "
                    "Overrides the default 3-days-out window. Ignored if `date` is set.",
    ),
    include: Literal["markets"] | None = Query(default=None),
):
    # Window semantics (date wins → explicit bounds → 3h-ago/3-days-out
    # default) live in EventQueries._list_stmt. MDProject's pick popup
    # passes starts_after/starts_before to widen past the default.
    filters = EventListFilters(
        sport=sport,
        league=league,
        live=live,
        date=date,
        starts_after=starts_after,
        starts_before=starts_before,
    )
    total, rows = await _queries.list_window(
        session, filters,
        offset=page_params.offset, limit=page_params.page_size,
    )

    public_base = get_settings().public_base_url
    if include == "markets":
        items: list[EventWithMarketsOut] = await _attach_markets(session, rows, public_base=public_base)
    else:
        # Don't validate-from-attribute — Event.markets is lazy="raise" and
        # would blow up. Build via the markets-less view, then wrap.
        items = [
            EventWithMarketsOut(**_event_out(r, public_base).model_dump(), markets=[])
            for r in rows
        ]

    page = Page.build(
        items=items,
        page=page_params.page,
        page_size=page_params.page_size,
        total=total,
    )
    return cached_json(page, response, if_none_match, max_age=LIST_MAX_AGE)


@router.get("/{event_id}")
async def get_event(
    session: SessionDep,
    response: Response,
    event_id: str,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    include: Literal["markets"] | None = Query(default=None),
):
    event = await _queries.get_detail(session, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")

    markets: list[MarketOut] = []
    public_base = get_settings().public_base_url
    if include == "markets":
        enriched = await _attach_markets(session, [event], public_base=public_base)
        markets = enriched[0].markets

    base = _event_out(event, public_base)
    payload = EventDetailOut(
        **base.model_dump(),
        markets=markets,
        odds_meta=OddsMeta(
            stale=compute_stale(
                status_type=event.status_type,
                start_time=event.start_time,
                grace_hours=get_settings().lifecycle_stale_grace_hours,
            ),
            last_provider_refresh_at=event.last_provider_refresh_at,
        ),
    )
    return cached_json(payload, response, if_none_match, max_age=DETAIL_MAX_AGE)


@router.get("/{event_id}/markets")
async def get_event_markets(
    session: SessionDep,
    event_id: str,
    category: str | None = Query(default=None, description="comma-separated"),
    scope: str | None = Query(default=None, description="comma-separated"),
    scope_subject: Literal["game", "team"] | None = Query(default=None),
    type: str | None = Query(default=None),
    live: bool | None = Query(default=None),
    team_id: str | None = Query(default=None),
    settled: bool | None = Query(default=None),
    min_decimal: str | None = Query(default=None),
    max_decimal: str | None = Query(default=None),
) -> dict:
    event = await _queries.get(session, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")

    filters = MarketFilters(
        category=category,
        scope=scope,
        scope_subject=scope_subject,
        type=type,
        live=live,
        team_id=team_id,
        settled=settled,
        min_decimal=min_decimal,
        max_decimal=max_decimal,
    )
    public_base = get_settings().public_base_url
    try:
        rows = await _queries.markets_for_event(session, event_id, filters)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return {
        "markets": [_market_out(m, public_base) for m in rows],
        "odds_meta": {
            "stale": compute_stale(
                status_type=event.status_type,
                start_time=event.start_time,
                grace_hours=get_settings().lifecycle_stale_grace_hours,
            ),
            "last_provider_refresh_at": event.last_provider_refresh_at,
        },
    }


# ---- helpers ---------------------------------------------------------------


async def _attach_markets(
    session, events: list[Event], *, public_base: str,
) -> list[EventWithMarketsOut]:
    """Bulk-load markets+selections for a list of events, return enriched dtos."""
    if not events:
        return []
    markets = await _queries.markets_for_events(session, [e.id for e in events])
    by_event: dict[str, list[Market]] = {}
    for m in markets:
        by_event.setdefault(m.event_id, []).append(m)

    out: list[EventWithMarketsOut] = []
    for e in events:
        body = _event_out(e, public_base).model_dump()
        out.append(EventWithMarketsOut(
            **body,
            markets=[_market_out(m, public_base) for m in by_event.get(e.id, [])],
        ))
    return out
