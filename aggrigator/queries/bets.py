"""Tenant-scoped bet queries (plan §6.8 win #1).

Every method applies ``Bet.tenant_user_id == self.tenant_user_id``
itself — a handler physically cannot forget the scope, and a future
endpoint built on this class cannot leak cross-tenant rows. The
constructor refuses a missing tenant id outright.

Routes obtain an instance via the ``Depends`` provider in
``api/bets.py``, which builds it from the *acting* tenant user
(``deps.require_acting_tenant_user``) — the key's owner today, the
service-key-asserted user after the Phase 2 cutover.
"""

from __future__ import annotations

import uuid
from datetime import date as Date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.models import Bet, BetSettlementStatus


def _bod(d: Date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


class BetQueries:
    """Stateless re: session (passed per call); stateful re: tenant scope."""

    def __init__(self, tenant_user_id: uuid.UUID):
        if not tenant_user_id:
            raise ValueError("BetQueries requires a tenant_user_id — unscoped access is not a thing")
        self.tenant_user_id = tenant_user_id

    # -- internals ----------------------------------------------------------

    def _base(self):
        return select(Bet).where(Bet.tenant_user_id == self.tenant_user_id)

    @staticmethod
    def _window(stmt, from_: Date | None, to: Date | None):
        if from_ is not None:
            stmt = stmt.where(Bet.placed_at >= _bod(from_))
        if to is not None:
            stmt = stmt.where(Bet.placed_at < _bod(to) + timedelta(days=1))
        return stmt

    @staticmethod
    def _status(stmt, status_filter: str | None):
        """``open`` / ``settled`` / ``all`` / raw status code / None."""
        if not status_filter or status_filter == "all":
            return stmt
        if status_filter == "open":
            return stmt.where(Bet.settlement_status == BetSettlementStatus.OPEN.value)
        if status_filter == "settled":
            return stmt.where(Bet.settlement_status != BetSettlementStatus.OPEN.value)
        return stmt.where(Bet.settlement_status == status_filter)

    # -- reads --------------------------------------------------------------

    async def list_newest_first(
        self,
        session: AsyncSession,
        *,
        status_filter: str | None = None,
        from_: Date | None = None,
        to: Date | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Bet]:
        stmt = (
            self._base()
            .order_by(Bet.placed_at.desc(), Bet.id)
            .limit(limit)
            .offset(offset)
        )
        stmt = self._status(stmt, status_filter)
        stmt = self._window(stmt, from_, to)
        return list(await session.scalars(stmt))

    async def list_chronological(
        self,
        session: AsyncSession,
        *,
        from_: Date | None = None,
        to: Date | None = None,
    ) -> list[Bet]:
        """Oldest-first full window — feeds the summary/equity-curve walk."""
        stmt = self._base().order_by(Bet.placed_at.asc(), Bet.id)
        stmt = self._window(stmt, from_, to)
        return list(await session.scalars(stmt))

    async def get_owned(self, session: AsyncSession, bet_id: str) -> Bet | None:
        """None on miss OR cross-tenant attempt — indistinguishable to the
        caller by design (no enumeration oracle)."""
        return await session.scalar(self._base().where(Bet.id == bet_id))

    # -- writes -------------------------------------------------------------

    async def create(self, session: AsyncSession, **fields) -> Bet:
        """Insert a bet for THIS tenant. ``tenant_user_id`` is not an
        accepted field — the scope is the class's, not the caller's."""
        fields.pop("tenant_user_id", None)
        bet = Bet(
            id=str(uuid.uuid4()),
            tenant_user_id=self.tenant_user_id,
            **fields,
        )
        session.add(bet)
        await session.flush()
        return bet

    async def delete(self, session: AsyncSession, bet: Bet) -> None:
        await session.delete(bet)
        await session.flush()
