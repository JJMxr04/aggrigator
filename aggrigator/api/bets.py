"""Bet tracking — CRUD + summary endpoints under ``/v1/analytics/bets``.

User-private ledger (file 07 of the dashboard plan). PRO-gated like the
rest of analytics. Every row is filtered by ``tenant_user.id`` so cross-
tenant reads are structurally impossible; no admin override path here.

Auto-settle on event finalization lives in
``aggrigator/ingest/bet_autosettle.py`` (Phase D.3) — these endpoints
only handle user-driven CRUD.
"""

from __future__ import annotations

import uuid
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from aggrigator.deps import SessionDep, require_pro_user
from aggrigator.models import Bet, BetSettlementStatus, TenantUser
from aggrigator.schemas.bet import BetCreate, BetOut, BetUpdate

router = APIRouter(
    prefix="/v1/analytics/bets",
    tags=["analytics", "bets"],
    dependencies=[Depends(require_pro_user)],
)

CurrentUser = Annotated[TenantUser, Depends(require_pro_user)]


def _bod(d: Date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


@router.get("", response_model=list[BetOut])
async def list_bets(
    session: SessionDep,
    tenant_user: CurrentUser,
    status_filter: str | None = Query(default=None, alias="status"),
    from_: Date | None = Query(default=None, alias="from"),
    to: Date | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[Bet]:
    """List the caller's bets, newest first. ``status`` accepts
    ``open`` / ``settled`` / ``all`` plus the raw status codes."""
    stmt = (
        select(Bet)
        .where(Bet.tenant_user_id == tenant_user.id)
        .order_by(Bet.placed_at.desc(), Bet.id)
        .limit(limit)
        .offset(offset)
    )
    stmt = _apply_status_filter(stmt, status_filter)
    if from_ is not None:
        stmt = stmt.where(Bet.placed_at >= _bod(from_))
    if to is not None:
        stmt = stmt.where(Bet.placed_at < _bod(to) + timedelta(days=1))
    return list(await session.scalars(stmt))


@router.post("", response_model=BetOut, status_code=status.HTTP_201_CREATED)
async def create_bet(
    session: SessionDep,
    tenant_user: CurrentUser,
    body: BetCreate,
) -> Bet:
    """Insert a new bet. ``placed_at`` defaults to now if omitted."""
    placed_at = body.placed_at or datetime.now(tz=timezone.utc)
    bet = Bet(
        id=str(uuid.uuid4()),
        tenant_user_id=tenant_user.id,
        placed_at=placed_at,
        event_id=body.event_id or None,
        label=body.label,
        selection_type=body.selection_type,
        bookmaker=body.bookmaker or None,
        stake=Decimal(body.stake),
        decimal_odds=Decimal(body.decimal_odds),
        settlement_status=BetSettlementStatus.OPEN.value,
        note=body.note or None,
    )
    session.add(bet)
    await session.flush()
    return bet


@router.patch("/{bet_id}", response_model=BetOut)
async def update_bet(
    session: SessionDep,
    tenant_user: CurrentUser,
    bet_id: str,
    body: BetUpdate,
) -> Bet:
    """Edit settlement_status / payout / note / settled_at.

    Settlement transitions are user-driven here — the auto-settle hook
    is separate (``ingest/bet_autosettle.py``). If the user moves the
    bet out of OPEN, ``settled_at`` defaults to now when not supplied.
    Recomputes ``payout`` from stake × decimal_odds when settling to
    WON and the body didn't override it — keeps the math consistent
    with the auto-settle path."""
    bet = await _get_owned_bet(session, tenant_user.id, bet_id)

    new_status = body.settlement_status
    if body.settlement_status is not None:
        bet.settlement_status = body.settlement_status
        if body.settlement_status == BetSettlementStatus.OPEN.value:
            bet.settled_at = None
            bet.payout = None
        else:
            bet.settled_at = body.settled_at or datetime.now(tz=timezone.utc)
            if body.payout is not None:
                bet.payout = Decimal(body.payout)
            elif new_status == BetSettlementStatus.WON.value:
                bet.payout = (bet.stake * bet.decimal_odds).quantize(Decimal("0.01"))
            elif new_status in (BetSettlementStatus.PUSH.value, BetSettlementStatus.VOID.value):
                bet.payout = bet.stake
            elif new_status == BetSettlementStatus.LOST.value:
                bet.payout = Decimal("0.00")
    elif body.payout is not None:
        bet.payout = Decimal(body.payout)

    if body.note is not None:
        bet.note = body.note
    if body.settled_at is not None:
        bet.settled_at = body.settled_at

    await session.flush()
    return bet


@router.delete("/{bet_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bet(
    session: SessionDep,
    tenant_user: CurrentUser,
    bet_id: str,
) -> None:
    """Hard-delete. The bet log is user-owned and not consumed by any
    shared analytics, so soft-delete carries no benefit in v1."""
    bet = await _get_owned_bet(session, tenant_user.id, bet_id)
    await session.delete(bet)
    await session.flush()


@router.get("/summary")
async def bets_summary(
    session: SessionDep,
    tenant_user: CurrentUser,
    from_: Date | None = Query(default=None, alias="from"),
    to: Date | None = Query(default=None),
) -> dict:
    """Aggregates for the dashboard summary card + equity curve + ROI
    by bucket. One query, builds derived fields in Python.

    Equity-curve points are returned as ``[{placed_at, cumulative_pl}]``
    in chronological order (oldest first) so the line chart renders
    left-to-right. Open bets are excluded from cumulative P/L — they
    haven't resolved — but counted separately so the template can
    render a "shadow" trailing the curve."""
    stmt = (
        select(Bet)
        .where(Bet.tenant_user_id == tenant_user.id)
        .order_by(Bet.placed_at.asc(), Bet.id)
    )
    if from_ is not None:
        stmt = stmt.where(Bet.placed_at >= _bod(from_))
    if to is not None:
        stmt = stmt.where(Bet.placed_at < _bod(to) + timedelta(days=1))
    bets = list(await session.scalars(stmt))

    total = len(bets)
    open_count = wins = losses = pushes = voids = 0
    total_staked = Decimal("0.00")
    total_payout = Decimal("0.00")
    settled_excl_push = 0

    equity_curve: list[dict] = []
    running_pl = Decimal("0.00")

    for b in bets:
        status_ = b.settlement_status
        if status_ == BetSettlementStatus.OPEN.value:
            open_count += 1
            continue
        total_staked += b.stake
        payout = b.payout if b.payout is not None else Decimal("0.00")
        total_payout += payout
        pl = payout - b.stake
        running_pl += pl
        equity_curve.append({
            "placed_at": b.placed_at,
            "bet_id": b.id,
            "cumulative_pl": float(running_pl),
        })
        if status_ == BetSettlementStatus.WON.value:
            wins += 1
            settled_excl_push += 1
        elif status_ == BetSettlementStatus.LOST.value:
            losses += 1
            settled_excl_push += 1
        elif status_ == BetSettlementStatus.PUSH.value:
            pushes += 1
        elif status_ == BetSettlementStatus.VOID.value:
            voids += 1

    settled = wins + losses + pushes + voids
    hit_rate = (wins / settled_excl_push) if settled_excl_push else None
    total_profit = total_payout - total_staked
    roi = (total_profit / total_staked) if total_staked > 0 else None

    return {
        "totals": {
            "total_bets": total,
            "settled": settled,
            "open": open_count,
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "voids": voids,
            "total_staked": float(total_staked),
            "total_payout": float(total_payout),
            "total_profit": float(total_profit),
            "hit_rate": hit_rate,
            "roi": float(roi) if roi is not None else None,
        },
        "equity_curve": equity_curve,
        "roi_by_bucket": _roi_by_bucket(bets),
    }


def _roi_by_bucket(bets: list[Bet]) -> dict:
    """ROI partitioned by selection_type, bookmaker, league (via event_id
    prefix), and decimal-odds band. Cheap O(N) walk; called only for
    the summary endpoint which is itself paginated/windowed."""
    buckets: dict[str, dict[str, dict[str, Decimal]]] = {
        "selection_type": {},
        "bookmaker": {},
        "odds_band": {},
    }

    def _add(group: str, key: str, stake: Decimal, payout: Decimal) -> None:
        b = buckets[group].setdefault(key, {"staked": Decimal("0"), "payout": Decimal("0")})
        b["staked"] += stake
        b["payout"] += payout

    for bet in bets:
        if bet.settlement_status == BetSettlementStatus.OPEN.value:
            continue
        payout = bet.payout if bet.payout is not None else Decimal("0.00")
        _add("selection_type", bet.selection_type or "unknown", bet.stake, payout)
        _add("bookmaker", (bet.bookmaker or "unspecified"), bet.stake, payout)
        _add("odds_band", _odds_band(bet.decimal_odds), bet.stake, payout)

    def _to_roi(table: dict) -> list[dict]:
        return sorted(
            [
                {
                    "key": k,
                    "staked": float(v["staked"]),
                    "payout": float(v["payout"]),
                    "profit": float(v["payout"] - v["staked"]),
                    "roi": (
                        float((v["payout"] - v["staked"]) / v["staked"])
                        if v["staked"] > 0 else None
                    ),
                    "n": None,  # n is per-row; the dashboard mostly cares about
                                # ROI + staked. Skipping the count keeps the
                                # payload narrow.
                }
                for k, v in table.items()
            ],
            key=lambda r: (r["roi"] if r["roi"] is not None else -999),
            reverse=True,
        )

    return {
        "selection_type": _to_roi(buckets["selection_type"]),
        "bookmaker": _to_roi(buckets["bookmaker"]),
        "odds_band": _to_roi(buckets["odds_band"]),
    }


def _odds_band(decimal_odds: Decimal) -> str:
    if decimal_odds is None:
        return "unknown"
    if decimal_odds < Decimal("2.0"):
        return "<2.0"
    if decimal_odds < Decimal("3.0"):
        return "2.0-3.0"
    if decimal_odds < Decimal("5.0"):
        return "3.0-5.0"
    return "5.0+"


def _apply_status_filter(stmt, status_filter: str | None):
    if not status_filter or status_filter == "all":
        return stmt
    if status_filter == "open":
        return stmt.where(Bet.settlement_status == BetSettlementStatus.OPEN.value)
    if status_filter == "settled":
        return stmt.where(Bet.settlement_status != BetSettlementStatus.OPEN.value)
    # Otherwise treat as a raw status code (won / lost / push / void).
    return stmt.where(Bet.settlement_status == status_filter)


async def _get_owned_bet(session, tenant_user_id, bet_id: str) -> Bet:
    """Look up a bet by id, scoped to the caller's tenant. 404 on miss
    OR on cross-tenant attempt — the two are indistinguishable to the
    caller, which is intentional (no enumeration oracle)."""
    bet = await session.scalar(
        select(Bet).where(Bet.id == bet_id, Bet.tenant_user_id == tenant_user_id)
    )
    if bet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bet not found")
    return bet
