"""High-level ingest orchestration.

Composes the pure pipeline (``normalize`` -> ``upsert_team`` ->
``upsert_event`` -> ``write_markets`` -> ``grade_event``) and reports the
lifecycle transition so the caller can decide whether to enqueue an outbound
webhook.

Async port of MDProject's ``EventCron._persist`` flow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.ingest.client import SgoClient
from aggrigator.ingest.ingester import write_markets
from aggrigator.ingest.lifecycle import EventState, Transition, decide_transition
from aggrigator.ingest.normalize import EventSpec, event_spec_from_payload
from aggrigator.ingest.settlement_computed import settle_event, void_remaining_pending
from aggrigator.ingest.settlement_provider import grade_event
from aggrigator.ingest.upserts import (
    upsert_event_from_spec,
    upsert_team_from_spec,
)
from aggrigator.models import Event, League
from aggrigator.webhooks.enqueue import enqueue_for_event

logger = logging.getLogger(__name__)


class IngestResult(NamedTuple):
    """What ``ingest_event`` returns to its caller (cron task / webhook
    enqueuer / test)."""
    event: Event
    transition: Transition
    selections_written: int
    selections_graded: int       # PROVIDER (per-odd ``score`` from SGO)
    selections_computed: int     # COMPUTED fallback (event-score logic)
    selections_voided: int       # PENDING → VOID at finalize (definitive only)
    deliveries_enqueued: int


@dataclass
class LeagueReport:
    league_id: str
    events_processed: int = 0
    events_failed: int = 0
    transitions: list[Transition] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.transitions is None:
            self.transitions = []


async def ingest_event(
    session: AsyncSession, payload: dict
) -> IngestResult | None:
    """Normalize one SGO event payload, upsert the row, write its markets,
    PROVIDER-grade if finalized, return the lifecycle transition.

    The caller is responsible for committing the session — this function only
    issues writes and ``flush`` calls. Idempotent on repeated input.
    """
    spec = event_spec_from_payload(payload)
    if spec is None:
        return None  # non-match event or malformed

    home = await upsert_team_from_spec(session, spec.home_team)
    away = await upsert_team_from_spec(session, spec.away_team)
    if home is None or away is None:
        # Missing league row → skip rather than corrupt. Seeding leagues is
        # the cron-runner's responsibility (see Phase 1.5 seeder).
        logger.warning(
            "Skipping event %s: league %s not seeded",
            spec.event_id, spec.league_id,
        )
        return None
    await session.flush()

    upserted = await upsert_event_from_spec(session, spec, home=home, away=away)
    event = upserted.event
    await session.flush()

    selections_written = await write_markets(session, event, spec.markets)

    selections_graded = 0
    selections_computed = 0
    selections_voided = 0
    if event.is_finalized:
        # 1) PROVIDER first — per-odd ``score`` field from SGO. Covers
        #    MONEYLINE / SPREAD / TOTAL / PROPS_TEAM / yn+eo PROPS_GAME.
        selections_graded = await grade_event(session, event, spec.markets)
        # 2) COMPUTED fallback for whatever PROVIDER skipped (soccer BTTS /
        #    double-chance / draw-no-bet / SOCCER+NHL team totals — all
        #    derivable from event scores, no per-odd score needed). Flush so
        #    grade_event's UPDATEs are visible before settle_event reads.
        #    settle_event itself only touches PENDING rows whose source is
        #    '' or COMPUTED — never overrides a PROVIDER-graded row, never
        #    touches MANUAL.
        await session.flush()
        selections_computed = await settle_event(session, event)
        # 3) Lock-in pass: anything still PENDING on a finished event is
        #    something we lack data to resolve (e.g. quarter-by-quarter
        #    moneylines without per-period scores on the free SGO tier).
        #    Mark VOID so the webhook payload is definitive — no PENDING
        #    selections sneak out attached to an ``event.finalized`` event.
        await session.flush()
        selections_voided = await void_remaining_pending(session, event)

    new_state = EventState(
        status_type=spec.status_type,
        home_score=spec.home_score,
        away_score=spec.away_score,
    )
    transition = decide_transition(upserted.previous_state, new_state)

    deliveries: list = []
    if transition != Transition.NONE:
        # Flush so markets/selections/settlements are visible to the enqueue
        # query before we read them into the payload.
        await session.flush()
        deliveries = await enqueue_for_event(session, event, transition)

    return IngestResult(
        event=event,
        transition=transition,
        selections_written=selections_written,
        selections_graded=selections_graded,
        selections_computed=selections_computed,
        selections_voided=selections_voided,
        deliveries_enqueued=len(deliveries),
    )


async def ingest_league(
    session: AsyncSession, league: League, client: SgoClient,
) -> LeagueReport:
    """Pull events for one league, ingest each, return a report."""
    report = LeagueReport(league_id=league.id)
    for payload in client.get_events(
        league_id=league.id,
        include_open_close=True,
        odds_available=None,
        limit=50,
    ):
        try:
            result = await ingest_event(session, payload)
        except Exception:  # noqa: BLE001
            logger.exception("ingest failed for event=%s", payload.get("eventID"))
            report.events_failed += 1
            continue
        if result is None:
            continue
        report.events_processed += 1
        report.transitions.append(result.transition)
        await session.flush()
    league.last_refreshed_at = datetime.now(tz=timezone.utc)
    return report


async def ingest_due_leagues(
    session: AsyncSession, client: SgoClient,
) -> list[LeagueReport]:
    """Walk every active League in the DB and ingest each one. Cadence-gating
    is intentionally omitted for v1 — the ARQ cron decides scheduling. Inside
    a single call, every active league is touched once."""
    from sqlalchemy import select

    leagues = list(await session.scalars(
        select(League).where(League.active.is_(True))
    ))
    reports: list[LeagueReport] = []
    for league in leagues:
        reports.append(await ingest_league(session, league, client))
    return reports
