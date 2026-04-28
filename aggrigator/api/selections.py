"""``/v1/selections/{id}/movement`` and ``/v1/slips``.

Movement returns the OddsQuote time-series for a selection. Slips is a
stateless parlay combiner — combine N selection prices into one decimal.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from aggrigator.deps import SessionDep, require_user
from aggrigator.models import OddsQuote, Selection, User
from aggrigator.schemas.selection import QuoteOut, SelectionMovementOut
from aggrigator.schemas.slip import SlipLegOut, SlipsIn, SlipsOut

router = APIRouter(prefix="/v1", tags=["selections"])


@router.get("/selections/{selection_id}/movement", response_model=SelectionMovementOut)
async def selection_movement(
    session: SessionDep,
    _user: Annotated[User, Depends(require_user)],
    selection_id: str,
    since: datetime | None = Query(default=None),
) -> SelectionMovementOut:
    sel = await session.get(Selection, selection_id)
    if sel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "selection not found")

    cutoff = since or (datetime.now(tz=timezone.utc) - timedelta(hours=24))
    rows = list(await session.scalars(
        select(OddsQuote)
        .where(OddsQuote.selection_id == sel.id, OddsQuote.captured_at >= cutoff)
        .order_by(OddsQuote.captured_at)
    ))
    return SelectionMovementOut(
        selection_id=sel.id,
        quotes=[QuoteOut(decimal_odds=q.decimal_odds, captured_at=q.captured_at) for q in rows],
    )


@router.post("/slips", response_model=SlipsOut, status_code=status.HTTP_201_CREATED)
async def combine_slip(
    payload: SlipsIn,
    session: SessionDep,
    _user: Annotated[User, Depends(require_user)],
) -> SlipsOut:
    ids = [leg.selection_id for leg in payload.legs]
    rows = list(await session.scalars(
        select(Selection).where(Selection.id.in_(ids))
    ))
    by_id = {s.id: s for s in rows}

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
