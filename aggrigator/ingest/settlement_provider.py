"""PROVIDER-source settlement — graded from the provider's per-odd ``score`` field.

Runs after
``write_markets`` on a finalized event. Authoritative against COMPUTED, but
never overrides MANUAL.

For ML/SP/ML3WAY the grader needs both sides' scores so it walks the spec
list. For OU/YN/EO the per-odd ``score`` is sufficient.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.ingest.normalize import MarketSpec, SelectionSpec
from aggrigator.models import Event, Selection

logger = logging.getLogger(__name__)


async def grade_event(
    session: AsyncSession, event: Event, market_specs: Iterable[MarketSpec]
) -> int:
    """Walk a finalized event's market specs and PROVIDER-grade each selection.

    Returns count of selections graded. Idempotent: re-running on the same
    event leaves WON/LOST rows alone unless the underlying score changed.
    """
    if not event.is_finalized:
        return 0
    now = datetime.now(tz=timezone.utc)
    graded = 0
    for mspec in market_specs:
        graded += await _grade_market(session, mspec, now)
    if graded:
        logger.info("PROVIDER-graded %d selections on event=%s", graded, event.id)
    return graded


async def _grade_market(session: AsyncSession, mspec: MarketSpec, now: datetime) -> int:
    if mspec.category == "MONEYLINE":
        return await _grade_moneyline(session, mspec, now)
    if mspec.category == "SPREAD":
        return await _grade_spread(session, mspec, now)
    if mspec.category in ("TOTAL", "PROPS_TEAM"):
        return await _grade_totals(session, mspec, now)
    if mspec.category == "PROPS_GAME":
        return await _grade_props_game(session, mspec, now)
    return 0


async def _grade_moneyline(
    session: AsyncSession, mspec: MarketSpec, now: datetime
) -> int:
    by_side = {sel.raw_side_id: sel for sel in mspec.selections}
    home, away, draw = by_side.get("home"), by_side.get("away"), by_side.get("draw")
    if home is None or away is None:
        return 0
    h_score, a_score = home.score, away.score
    if h_score is None or a_score is None:
        return 0
    if h_score > a_score:
        return await _apply(session, [(home, "WON"), (away, "LOST"), (draw, "LOST")], now)
    if a_score > h_score:
        return await _apply(session, [(home, "LOST"), (away, "WON"), (draw, "LOST")], now)
    return await _apply(
        session, [(home, "LOST"), (away, "LOST"), (draw, "WON" if draw else None)], now,
    )


async def _grade_spread(
    session: AsyncSession, mspec: MarketSpec, now: datetime
) -> int:
    by_side = {sel.raw_side_id: sel for sel in mspec.selections}
    home, away = by_side.get("home"), by_side.get("away")
    if home is None or away is None or home.score is None or away.score is None:
        return 0
    if mspec.line is None:
        return 0
    margin = home.score - away.score
    adjusted = float(margin) + float(mspec.line)
    if adjusted > 0:
        return await _apply(session, [(home, "WON"), (away, "LOST")], now)
    if adjusted < 0:
        return await _apply(session, [(home, "LOST"), (away, "WON")], now)
    return await _apply(session, [(home, "PUSH"), (away, "PUSH")], now)


async def _grade_totals(
    session: AsyncSession, mspec: MarketSpec, now: datetime
) -> int:
    """OVER/UNDER markets — TOTAL or per-side PROPS_TEAM total."""
    by_side = {sel.raw_side_id: sel for sel in mspec.selections}
    over, under = by_side.get("over"), by_side.get("under")
    if over is None or under is None:
        return 0
    score = over.score if over.score is not None else under.score
    if score is None or mspec.line is None:
        return 0
    line = float(mspec.line)
    if score > line:
        return await _apply(session, [(over, "WON"), (under, "LOST")], now)
    if score < line:
        return await _apply(session, [(over, "LOST"), (under, "WON")], now)
    return await _apply(session, [(over, "PUSH"), (under, "PUSH")], now)


async def _grade_props_game(
    session: AsyncSession, mspec: MarketSpec, now: datetime
) -> int:
    """yn / eo — single-odd grading via the score field."""
    graded = 0
    for sel in mspec.selections:
        if sel.score is None:
            continue
        if sel.raw_side_id in ("yes", "no"):
            won = bool(int(sel.score))
            new_status = (
                "WON"
                if (won and sel.raw_side_id == "yes")
                or (not won and sel.raw_side_id == "no")
                else "LOST"
            )
        elif sel.raw_side_id in ("even", "odd"):
            is_even = int(sel.score) % 2 == 0
            new_status = (
                "WON"
                if (is_even and sel.raw_side_id == "even")
                or ((not is_even) and sel.raw_side_id == "odd")
                else "LOST"
            )
        else:
            continue
        graded += await _apply_one(session, sel, new_status, now)
    return graded


# ---- writers ---------------------------------------------------------------


async def _apply(
    session: AsyncSession,
    pairs: list[tuple[SelectionSpec | None, str | None]],
    now: datetime,
) -> int:
    graded = 0
    for spec, status in pairs:
        if spec is None or status is None:
            continue
        graded += await _apply_one(session, spec, status, now)
    return graded


async def _apply_one(
    session: AsyncSession, spec: SelectionSpec, status: str, now: datetime
) -> int:
    """Update one Selection row by its synthesized PK. Skips MANUAL rows."""
    result = await session.execute(
        update(Selection)
        .where(Selection.id == spec.selection_id, Selection.settlement_source != "MANUAL")
        .values(settlement_status=status, settled_at=now, settlement_source="PROVIDER")
    )
    return result.rowcount or 0
