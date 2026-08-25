"""Bet tracking — CRUD + summary endpoints under ``/v1/analytics/bets``.

User-private ledger. All DB access goes
through the tenant-scoped ``queries.BetQueries`` — built per
request from the *acting* tenant user, so cross-tenant reads are
impossible by construction. The tier gate moved to MDProject;
the key itself is still required (router-level ``require_tenant_user``).

Auto-settle on event finalization lives in
``aggrigator/ingest/bet_autosettle.py`` — these endpoints
only handle user-driven CRUD.
"""

from __future__ import annotations

from datetime import date as Date, datetime, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aggrigator.deps import (
    SessionDep,
    require_acting_tenant_user,
    require_tenant_user,
)
from aggrigator.models import Bet, BetSettlementStatus, TenantUser
from aggrigator.queries import BetQueries
from aggrigator.schemas.bet import BetCreate, BetOut, BetUpdate

# Router-level key requirement is the structural guarantee — a future
# endpoint added here without an explicit user dep is still keyed.
router = APIRouter(
    prefix="/v1/analytics/bets",
    tags=["analytics", "bets"],
    dependencies=[Depends(require_tenant_user)],
)


async def _bet_queries(
    acting: Annotated[TenantUser, Depends(require_acting_tenant_user)],
) -> BetQueries:
    return BetQueries(acting.id)


Bets = Annotated[BetQueries, Depends(_bet_queries)]


@router.get("", response_model=list[BetOut])
async def list_bets(
    session: SessionDep,
    bets: Bets,
    status_filter: str | None = Query(default=None, alias="status"),
    from_: Date | None = Query(default=None, alias="from"),
    to: Date | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[Bet]:
    """List the caller's bets, newest first. ``status`` accepts
    ``open`` / ``settled`` / ``all`` plus the raw status codes."""
    return await bets.list_newest_first(
        session,
        status_filter=status_filter,
        from_=from_,
        to=to,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=BetOut, status_code=status.HTTP_201_CREATED)
async def create_bet(
    session: SessionDep,
    bets: Bets,
    body: BetCreate,
) -> Bet:
    """Insert a new bet. ``placed_at`` defaults to now if omitted."""
    return await bets.create(
        session,
        placed_at=body.placed_at or datetime.now(tz=timezone.utc),
        event_id=body.event_id or None,
        label=body.label,
        selection_type=body.selection_type,
        bookmaker=body.bookmaker or None,
        stake=Decimal(body.stake),
        decimal_odds=Decimal(body.decimal_odds),
        settlement_status=BetSettlementStatus.OPEN.value,
        note=body.note or None,
    )


@router.patch("/{bet_id}", response_model=BetOut)
async def update_bet(
    session: SessionDep,
    bets: Bets,
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
    bet = await _owned_or_404(bets, session, bet_id)

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
    bets: Bets,
    bet_id: str,
) -> None:
    """Hard-delete. The bet log is user-owned and not consumed by any
    shared analytics, so soft-delete carries no benefit in v1."""
    bet = await _owned_or_404(bets, session, bet_id)
    await bets.delete(session, bet)


@router.get("/summary")
async def bets_summary(
    session: SessionDep,
    bets: Bets,
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
    rows = await bets.list_chronological(session, from_=from_, to=to)

    total = len(rows)
    open_count = wins = losses = pushes = voids = 0
    total_staked = Decimal("0.00")
    total_payout = Decimal("0.00")
    settled_excl_push = 0

    equity_curve: list[dict] = []
    running_pl = Decimal("0.00")

    for b in rows:
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
        "roi_by_bucket": _roi_by_bucket(rows),
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


async def _owned_or_404(bets: BetQueries, session, bet_id: str) -> Bet:
    """404 on miss OR cross-tenant attempt — indistinguishable to the
    caller, which is intentional (no enumeration oracle)."""
    bet = await bets.get_owned(session, bet_id)
    if bet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bet not found")
    return bet
