"""Selection reads (plan §6.8 step 2) — movement time-series + slip legs."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.models import OddsQuote, Selection


class SelectionQueries:
    """Stateless — pass the session per call."""

    async def get(self, session: AsyncSession, selection_id: str) -> Selection | None:
        return await session.get(Selection, selection_id)

    async def quotes_since(
        self, session: AsyncSession, selection_id: str, cutoff: datetime,
    ) -> list[OddsQuote]:
        return list(await session.scalars(
            select(OddsQuote)
            .where(
                OddsQuote.selection_id == selection_id,
                OddsQuote.captured_at >= cutoff,
            )
            .order_by(OddsQuote.captured_at)
        ))

    async def by_ids(
        self, session: AsyncSession, ids: list[str],
    ) -> dict[str, Selection]:
        if not ids:
            return {}
        rows = await session.scalars(select(Selection).where(Selection.id.in_(ids)))
        return {s.id: s for s in rows}
