"""Seed Sport / League rows from the upstream provider.

The event-ingest cron (``ingest_due_leagues``) walks every active League
whose parent Sport is also active. The rows have to exist first — that's
what this module is for. Run it once per deploy and again whenever the
provider adds a sport / league.

Sport-gating: ``seed_leagues`` only persists leagues whose parent Sport
already exists in our DB AND is ``active=True``. Sports default to
inactive — an operator flips a sport on in SQLAdmin, re-runs seed, and
that sport's leagues come along already active by default.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.ingest.client import SgoClient
from aggrigator.ingest.upserts import upsert_league_from_sgo, upsert_sport_from_sgo
from aggrigator.models import Sport

logger = logging.getLogger(__name__)


async def seed_sports(
    session: AsyncSession, client: SgoClient, *, _payloads: list[dict] | None = None,
) -> int:
    """Upsert every Sport the upstream provider knows about. Returns count.

    ``_payloads`` lets a caller (``seed_all``) pass pre-fetched data so we
    don't make the same provider call twice. Public callers ignore it.

    New sports land ``active=False`` — operator must flip them on before
    their leagues qualify for ingest (which requires active league AND
    active sport).
    """
    payloads = _payloads if _payloads is not None else list(client.get_sports())
    count = 0
    for payload in payloads:
        if await upsert_sport_from_sgo(session, payload) is not None:
            count += 1
    await session.flush()
    logger.info("seeded %d sports", count)
    return count


async def seed_leagues(
    session: AsyncSession, client: SgoClient, *, _payloads: list[dict] | None = None,
) -> int:
    """Upsert leagues whose parent Sport is active in our DB. Idempotent.

    Fetches the full league list from the provider (one call on both SGO
    and oddsapi-via-adapter), then filters client-side to only the
    sports an operator has explicitly enabled. League payloads whose
    sport is unknown / inactive are skipped silently — re-run seed
    after flipping a sport on to bring its leagues in.

    New leagues land ``active=True`` by default (gated upstream by their
    Sport's active flag); operator-flipped leagues stick across re-seeds.
    """
    payloads = _payloads if _payloads is not None else list(client.get_leagues())

    active_sport_ids = set(
        await session.scalars(
            select(Sport.id).where(Sport.active.is_(True))
        )
    )
    if not active_sport_ids:
        logger.info(
            "seed_leagues: zero active sports — skipping league seed "
            "(enable a sport in SQLAdmin then re-run)",
        )
        return 0

    count = 0
    skipped = 0
    for payload in payloads:
        sport_id = payload.get("sportID")
        if sport_id not in active_sport_ids:
            skipped += 1
            continue
        if await upsert_league_from_sgo(session, payload) is not None:
            count += 1
    await session.flush()
    logger.info(
        "seeded %d leagues (skipped %d from inactive sports — "
        "active sports: %s)",
        count, skipped, sorted(active_sport_ids),
    )
    return count


async def seed_all(session: AsyncSession, client: SgoClient) -> dict:
    """Pre-fetch sports + leagues once and feed them into the upsert helpers
    so we never round-trip the same provider endpoint twice in one seed run.

    Order matters: seed_sports must commit first so seed_leagues' sport-
    activation filter sees fresh rows. The caller is responsible for the
    commit between them — we ``flush`` inside each helper so the SELECT
    in seed_leagues sees the inserted sports.
    """
    sport_payloads = list(client.get_sports())
    league_payloads = list(client.get_leagues())
    sports = await seed_sports(session, client, _payloads=sport_payloads)
    leagues = await seed_leagues(session, client, _payloads=league_payloads)
    return {"sports": sports, "leagues": leagues}
