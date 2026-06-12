"""``/v1/selections/{id}/movement`` and ``/v1/slips``.

Movement returns the OddsQuote time-series for a selection. Slips is a
stateless parlay combiner — combine N selection prices into one decimal.
Keyed reads (plan §6.2 P0-5): nothing in the aggregator is anonymous —
enforcement is flag-staged via ``keyed_reads_gate``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aggrigator.deps import SessionDep, keyed_reads_gate
from aggrigator.queries.selections import SelectionQueries
from aggrigator.schemas.selection import QuoteOut, SelectionMovementOut
from aggrigator.schemas.slip import SlipLegOut, SlipsIn, SlipsOut

# Router-level so a future endpoint can't be added keyless by accident.
router = APIRouter(
    prefix="/v1", tags=["selections"], dependencies=[Depends(keyed_reads_gate)],
)

_queries = SelectionQueries()


@router.get("/selections/{selection_id}/movement", response_model=SelectionMovementOut)
async def selection_movement(
    session: SessionDep,
    selection_id: str,
    since: datetime | None = Query(default=None),
) -> SelectionMovementOut:
    sel = await _queries.get(session, selection_id)
    if sel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "selection not found")

    cutoff = since or (datetime.now(tz=timezone.utc) - timedelta(hours=24))
    rows = await _queries.quotes_since(session, sel.id, cutoff)
    return SelectionMovementOut(
        selection_id=sel.id,
        quotes=[QuoteOut(decimal_odds=q.decimal_odds, captured_at=q.captured_at) for q in rows],
    )


@router.post("/slips", response_model=SlipsOut, status_code=status.HTTP_201_CREATED)
async def combine_slip(
    payload: SlipsIn,
    session: SessionDep,
) -> SlipsOut:
    ids = [leg.selection_id for leg in payload.legs]
    by_id = await _queries.by_ids(session, ids)

    missing = [sid for sid in ids if sid not in by_id]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"unknown selection ids: {missing}"
        )

    legs_out: list[SlipLegOut] = []
    combined = Decimal("1.0000")
    for sid in ids:
        sel = by_id[sid]
        if sel.decimal_odds is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"selection {sid} has no current price"
            )
        combined *= sel.decimal_odds
        legs_out.append(SlipLegOut(
            selection_id=sel.id,
            market_id=sel.market_id,
            type=sel.type,
            label=sel.label,
            decimal_odds=sel.decimal_odds,
        ))

    return SlipsOut(
        slip_id=uuid.uuid4(),
        legs=legs_out,
        combined_decimal=combined.quantize(Decimal("0.0001")),
    )
