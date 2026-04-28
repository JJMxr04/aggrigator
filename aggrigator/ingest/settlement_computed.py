"""COMPUTED-source settlement — derived from event scores.

Async port of MDProject's ``core/event/odds/settlement.py``. Runs as a fallback
when PROVIDER hasn't graded a selection (e.g., on events finalized but with
``score`` not yet shipped on individual odds). Source priority:
``MANUAL > PROVIDER > COMPUTED > PENDING`` — never downgrade.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from aggrigator.models import Event, Market, Selection

logger = logging.getLogger(__name__)


SOURCE_PRIORITY = {"MANUAL": 3, "PROVIDER": 2, "COMPUTED": 1, "": 0}


# ---- per-category functions — mutate Selection.settlement_status in place --


def _settle_moneyline(event: Event, market: Market, sels: Iterable[Selection]) -> None:
    winner_by_type = {1: "HOME", 2: "AWAY", 3: "DRAW"}
    winning_type = winner_by_type.get(event.winner_code or 0)
    if market.type == "NHL_MATCH_WINNER_REG":
        return  # needs regulation-only score, leave to PROVIDER
    if winning_type is None:
        return
    for sel in sels:
        if sel.type == winning_type:
            sel.settlement_status = "WON"
        elif sel.type in ("HOME", "AWAY", "DRAW"):
            sel.settlement_status = "LOST"


def _settle_total(event: Event, market: Market, sels: Iterable[Selection]) -> None:
    if event.home_score is None or event.away_score is None or market.line is None:
        return
    total = event.home_score + event.away_score
    line = float(market.line)
    for sel in sels:
        if sel.type == "OVER":
            sel.settlement_status = "WON" if total > line else "LOST" if total < line else "PUSH"
        elif sel.type == "UNDER":
            sel.settlement_status = "WON" if total < line else "LOST" if total > line else "PUSH"


def _settle_spread(event: Event, market: Market, sels: Iterable[Selection]) -> None:
    if event.home_score is None or event.away_score is None or market.line is None:
        return
    margin = event.home_score - event.away_score
    adjusted = margin + float(market.line)
    for sel in sels:
        if sel.type == "HOME":
            sel.settlement_status = "WON" if adjusted > 0 else "LOST" if adjusted < 0 else "PUSH"
        elif sel.type == "AWAY":
            sel.settlement_status = "WON" if adjusted < 0 else "LOST" if adjusted > 0 else "PUSH"


def _settle_props_game(event: Event, market: Market, sels: Iterable[Selection]) -> None:
    t = market.type

    if t in ("SOCCER_BTTS", "NHL_BTTS"):
        if event.home_score is None or event.away_score is None:
            return
        both_scored = event.home_score > 0 and event.away_score > 0
        for sel in sels:
            if sel.type == "YES":
                sel.settlement_status = "WON" if both_scored else "LOST"
            elif sel.type == "NO":
                sel.settlement_status = "LOST" if both_scored else "WON"
        return

    if t == "SOCCER_DOUBLE_CHANCE":
        wc = event.winner_code
        if wc is None:
            return
        win_set = {"1X": {1, 3}, "X2": {2, 3}, "12": {1, 2}}
        for sel in sels:
            bucket = win_set.get(sel.type)
            if bucket is None:
                continue
            sel.settlement_status = "WON" if wc in bucket else "LOST"
        return

    if t == "SOCCER_DRAW_NO_BET":
        wc = event.winner_code
        if wc is None:
            return
        if wc == 3:
            for sel in sels:
                if sel.type in ("HOME", "AWAY"):
                    sel.settlement_status = "PUSH"
            return
        winning_type = "HOME" if wc == 1 else "AWAY"
        for sel in sels:
            if sel.type == winning_type:
                sel.settlement_status = "WON"
            elif sel.type in ("HOME", "AWAY"):
                sel.settlement_status = "LOST"
        return


def _settle_props_team(event: Event, market: Market, sels: Iterable[Selection]) -> None:
    t = market.type
    if t in ("SOCCER_TEAM_TOTAL_GOALS", "NHL_TEAM_TOTAL_GOALS"):
        if market.line is None:
            return
        side = market.side
        score = (
            event.home_score if side == "HOME"
            else event.away_score if side == "AWAY"
            else None
        )
        if score is None:
            return
        line = float(market.line)
        for sel in sels:
            if sel.type == "OVER":
                sel.settlement_status = "WON" if score > line else "LOST" if score < line else "PUSH"
            elif sel.type == "UNDER":
                sel.settlement_status = "WON" if score < line else "LOST" if score > line else "PUSH"


SETTLEMENT_FUNCS = {
    "MONEYLINE": _settle_moneyline,
    "TOTAL": _settle_total,
    "SPREAD": _settle_spread,
    "PROPS_GAME": _settle_props_game,
    "PROPS_TEAM": _settle_props_team,
}


# ---- entry points ----------------------------------------------------------


async def settle_event(session: AsyncSession, event: Event) -> int:
    """Run computed settlement on every PENDING selection for ``event``.

    Only touches rows whose source is currently ``''`` (untouched) or
    ``COMPUTED``. Returns the number of selections updated.
    """
    if event.status_type != "finished":
        return 0

    now = datetime.now(tz=timezone.utc)
    total_updated = 0

    markets = list(await session.scalars(
        select(Market)
        .where(Market.event_id == event.id)
        .options(selectinload(Market.selections))
    ))
    for market in markets:
        fn = SETTLEMENT_FUNCS.get(market.category)
        if fn is None:
            continue

        touchable = [
            s for s in market.selections
            if SOURCE_PRIORITY.get(s.settlement_source, 0) <= SOURCE_PRIORITY["COMPUTED"]
            and s.settlement_status == "PENDING"
        ]
        if not touchable:
            continue

        original = {s.id: s.settlement_status for s in touchable}
        fn(event, market, touchable)

        for sel in touchable:
            if sel.settlement_status != original[sel.id]:
                sel.settled_at = now
                sel.settlement_source = "COMPUTED"
                total_updated += 1

    if total_updated:
        logger.info("Settled %d selections for event %s", total_updated, event.id)
    return total_updated


async def settle_pending_events(
    session: AsyncSession, lookback_hours: int = 48
) -> dict:
    """Backfill — finds finished events updated in the last ``lookback_hours``
    that still have PENDING selections, runs ``settle_event`` against each.

    Also voids any selection on a postponed/canceled event whose start_time is
    more than 7 days ago — same rule as MDProject's nightly backfill.
    """
    since = datetime.now(tz=timezone.utc) - timedelta(hours=lookback_hours)
    rows = list(await session.scalars(
        select(Event)
        .where(Event.status_type == "finished", Event.updated >= since)
    ))

    settled = 0
    processed = 0
    for event in rows:
        processed += 1
        settled += await settle_event(session, event)

    void_cutoff = datetime.now(tz=timezone.utc) - timedelta(days=7)
    voided_q = await session.execute(
        update(Selection)
        .where(
            Selection.settlement_status == "PENDING",
            Selection.market_id.in_(
                select(Market.id)
                .where(
                    Market.event_id.in_(
                        select(Event.id).where(
                            Event.start_time < void_cutoff,
                            Event.status_type.in_(["postponed", "canceled"]),
                        )
                    )
                )
            ),
        )
        .values(
            settlement_status="VOID",
            settled_at=datetime.now(tz=timezone.utc),
            settlement_source="COMPUTED",
        )
    )
    voided = voided_q.rowcount or 0
    if voided:
        logger.info("Voided %d stale pending selections", voided)

    return {"events_processed": processed, "selections_settled": settled, "voided": voided}
