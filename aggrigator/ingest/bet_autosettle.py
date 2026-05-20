"""Auto-settle open bets when an event flips to ``is_finalized``.

Fires from ``orchestrator.py`` after the existing market settlement
passes. Only touches bets whose ``selection_type`` we can resolve
purely from the event's ``winner_code`` — moneyline 3-way for soccer.
Spread / total / other markets need line context that bets don't carry
(file 07 deliberately left ``line`` off the table) and so stay open
for manual settlement.

Idempotent: the WHERE clause filters to ``settlement_status='open'``,
so re-running on an already-settled event is a no-op.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.models import Bet, BetSelectionType, BetSettlementStatus
from aggrigator.models.event import Event

logger = logging.getLogger(__name__)


_AUTO_SETTLABLE = {
    BetSelectionType.MONEYLINE_HOME.value,
    BetSelectionType.MONEYLINE_AWAY.value,
    BetSelectionType.MONEYLINE_DRAW.value,
}


def _settle_decision(selection_type: str, winner_code: int | None) -> str | None:
    """Map ``(selection_type, winner_code)`` → settlement status.

    Returns None when the bet's selection_type isn't in the
    auto-settlable set — caller leaves the bet open for manual
    settle. For moneyline bets on a finalized event with no
    recorded winner_code (data-quality miss), we VOID so the user
    gets their stake back rather than a silent loss."""
    if selection_type not in _AUTO_SETTLABLE:
        # total_*, spread_*, other — no line carried on Bet in v1.
        return None

    if winner_code is None:
        # Moneyline bet on a finalized event but no winner recorded.
        # Refund rather than guess.
        return BetSettlementStatus.VOID.value

    if selection_type == BetSelectionType.MONEYLINE_HOME.value:
        if winner_code == 1:
            return BetSettlementStatus.WON.value
        if winner_code == 2:
            return BetSettlementStatus.LOST.value
        if winner_code == 3:
            return BetSettlementStatus.PUSH.value
        return BetSettlementStatus.VOID.value

    if selection_type == BetSelectionType.MONEYLINE_AWAY.value:
        if winner_code == 2:
            return BetSettlementStatus.WON.value
        if winner_code == 1:
            return BetSettlementStatus.LOST.value
        if winner_code == 3:
            return BetSettlementStatus.PUSH.value
        return BetSettlementStatus.VOID.value

    # MONEYLINE_DRAW
    if winner_code == 3:
        return BetSettlementStatus.WON.value
    if winner_code in (1, 2):
        return BetSettlementStatus.LOST.value
    return BetSettlementStatus.VOID.value


def _payout_for(stake: Decimal, decimal_odds: Decimal, status_: str) -> Decimal:
    """Final payout amount for a settled bet.

    won → stake × odds (rounded to cents). lost → 0. push / void →
    stake refunded. Quantize keeps the column type happy (Numeric(12,2))."""
    cents = Decimal("0.01")
    if status_ == BetSettlementStatus.WON.value:
        return (stake * decimal_odds).quantize(cents)
    if status_ == BetSettlementStatus.LOST.value:
        return Decimal("0.00")
    # push or void — refund the stake.
    return stake.quantize(cents)


async def autosettle_bets_for_event(
    session: AsyncSession, event: Event,
) -> int:
    """Walk open bets attached to ``event`` and settle the ones whose
    selection_type maps to a definite outcome. Returns the count
    settled — for logging on the orchestrator side.

    The caller is responsible for ``await session.flush()`` /
    ``session.commit()`` — we leave the session in a dirty state so
    the orchestrator can batch with the surrounding work."""
    if not event.is_finalized:
        return 0

    bets = list(await session.scalars(
        select(Bet).where(
            Bet.event_id == event.id,
            Bet.settlement_status == BetSettlementStatus.OPEN.value,
        )
    ))
    if not bets:
        return 0

    now = datetime.now(tz=timezone.utc)
    settled = 0
    for bet in bets:
        decision = _settle_decision(bet.selection_type, event.winner_code)
        if decision is None:
            continue
        bet.settlement_status = decision
        bet.settled_at = now
        bet.payout = _payout_for(bet.stake, bet.decimal_odds, decision)
        settled += 1

    if settled:
        logger.info(
            "bet_autosettle: settled %d bet(s) on event %s (winner_code=%s)",
            settled, event.id, event.winner_code,
        )
    return settled
