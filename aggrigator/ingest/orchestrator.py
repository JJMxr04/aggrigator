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
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.config import get_settings
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
from aggrigator.ops.progress import raise_if_cancelled, set_progress
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
    session: AsyncSession,
    payload: dict,
    *,
    skip_if_new_terminal: bool = False,
    phase: str = "all",
) -> IngestResult | None:
    """Normalize one SGO event payload, upsert the row, write its markets,
    PROVIDER-grade if finalized, return the lifecycle transition.

    The caller is responsible for committing the session — this function only
    issues writes and ``flush`` calls. Idempotent on repeated input.

    When ``skip_if_new_terminal=True``, payloads for events not already in
    our DB whose ``status_type`` is terminal (``finished`` / ``postponed`` /
    ``canceled``) are dropped without inserting anything. The cron loop
    (``ingest_league``) sets this; the manual ad-hoc ingest endpoint
    leaves it off so operators can backfill on demand.

    ``phase`` controls which writes happen:

    - ``"all"`` (default): everything — upsert event + markets + selections +
      odds quotes + bookmaker quotes + grading + webhook enqueue. Slowest.
    - ``"lifecycle"``: upsert event row + status, run grading/settle/void on
      finalized events, enqueue webhooks. **Skip the entire markets walk.**
      Fast — useful when you only care about scores and lifecycle transitions
      and the per-bookmaker price snapshot can wait.
    - ``"odds"``: write only the markets/selections/odds-quotes/bookmaker-quotes
      for an existing event. Skip lifecycle, settle, webhooks. Requires the
      event row to already exist (lifecycle phase or full ``"all"`` walk
      must have created it). New events not in our DB are skipped.
    """
    spec = event_spec_from_payload(payload)
    if spec is None:
        return None  # non-match event or malformed

    if phase == "odds":
        # Odds-only: existing event row is required. We don't touch the
        # event metadata — just refresh the per-bookmaker prices.
        event = await session.get(Event, spec.event_id)
        if event is None:
            logger.debug(
                "odds phase: event %s not in DB yet "
                "(run lifecycle/all phase first to create the row)",
                spec.event_id,
            )
            return None
        # Even in odds-only mode, skip when finalized + scores haven't
        # moved — the prices on a final-and-graded game are immutable.
        if (
            event.is_finalized
            and (spec.status_type or "").lower() == "finished"
            and event.home_score == spec.home_score
            and event.away_score == spec.away_score
        ):
            return IngestResult(
                event=event, transition=Transition.NONE,
                selections_written=0, selections_graded=0,
                selections_computed=0, selections_voided=0,
                deliveries_enqueued=0,
            )
        selections_written = await write_markets(session, event, spec.markets)
        await session.flush()
        return IngestResult(
            event=event, transition=Transition.NONE,
            selections_written=selections_written,
            selections_graded=0, selections_computed=0, selections_voided=0,
            deliveries_enqueued=0,
        )

    # phase == "lifecycle" or "all" — full event upsert path
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

    upserted = await upsert_event_from_spec(
        session, spec, home=home, away=away,
        skip_if_new_terminal=skip_if_new_terminal,
    )
    if upserted is None:
        # New + already-terminal — see upsert_event_from_spec's docstring.
        return None
    event = upserted.event
    await session.flush()

    # Cheap exit for finalized-stable events: if our DB already has this
    # event in a finished state with the same scores AND SGO is reporting
    # the same finished state, the markets / selections / bookmaker quotes
    # below cannot have changed. Skip the (very expensive) write_markets
    # walk — the row's last_provider_refresh_at was already touched by
    # upsert_event_from_spec, which is all the freshness signal we need.
    #
    # This is the dominant savings on the rolling ingest window: once
    # an event is finalized, every subsequent walk re-fetches it for
    # ``AGG_INGEST_WINDOW_DAYS_BEHIND`` days. Without this skip we pay
    # for ~hundreds of bookmaker_selection upserts per event, daily.
    prev = upserted.previous_state
    finalized_stable = (
        prev is not None
        and prev.status_type == "finished"
        and (spec.status_type or "").lower() == "finished"
        and prev.home_score == spec.home_score
        and prev.away_score == spec.away_score
    )
    if finalized_stable:
        return IngestResult(
            event=event,
            transition=Transition.NONE,
            selections_written=0,
            selections_graded=0,
            selections_computed=0,
            selections_voided=0,
            deliveries_enqueued=0,
        )

    # Lifecycle phase deliberately skips write_markets — that's the
    # whole point. Status / settlement / webhook work below still runs.
    selections_written = 0
    if phase == "all":
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
    session: AsyncSession,
    league: League,
    client: SgoClient,
    *,
    phase: str = "all",
) -> LeagueReport:
    """Pull events for one league, ingest each, return a report.

    SGO bills 1 entity per event, so the per-month quota is driven by
    (events in window) × (walks per cycle) — NOT by markets/selections/
    bookmakers. Two genuine quota optimizations apply here:

    1. **Time window** (``AGG_INGEST_WINDOW_DAYS_BEHIND`` /
       ``..._AHEAD``): SGO is asked for events in
       ``[now - behind, now + ahead]`` only. Smaller window = fewer
       events returned = fewer entities billed.
    2. **``skip_if_new_terminal=True``**: anything inside that window
       SGO reports as already terminal but our DB has never seen gets
       dropped (no DB writes, but SGO has already counted the entity).
       Existing events still flow through their lifecycle
       (live → finished still triggers settlement).

    The ``include_alt_lines`` / ``odd_ids`` / ``bookmaker_id`` knobs
    below trim the response shape — they save bandwidth and DB write
    time, NOT entity quota.

    ``phase`` forwards to ``ingest_event`` — see its docstring for the
    semantics of ``"all"`` / ``"lifecycle"`` / ``"odds"``.
    """
    settings = get_settings()
    now = datetime.now(tz=timezone.utc)
    starts_after_iso = (
        now - timedelta(days=settings.ingest_window_days_behind)
    ).isoformat(timespec="seconds")
    starts_before_iso = (
        now + timedelta(days=settings.ingest_window_days_ahead)
    ).isoformat(timespec="seconds")

    odd_ids = [
        s.strip() for s in (settings.ingest_odd_ids or "").split(",") if s.strip()
    ] or None
    bookmaker_id = settings.ingest_bookmaker_id or None

    logger.info(
        "ingest_league %s: walking SGO events in window [%s, %s] "
        "(alt_lines=%s, odd_ids=%s, bookmaker=%s)",
        league.id, starts_after_iso, starts_before_iso,
        settings.ingest_include_alt_lines, odd_ids, bookmaker_id,
    )
    filters_summary_parts = []
    if not settings.ingest_include_alt_lines:
        filters_summary_parts.append("alt_lines=off")
    if odd_ids:
        filters_summary_parts.append(f"odd_ids={len(odd_ids)} filter")
    if bookmaker_id:
        # bookmaker_id may be a single ID or a CSV — show the count
        # for legibility when there's more than one.
        n_books = len([s for s in bookmaker_id.split(",") if s.strip()])
        if n_books == 1:
            filters_summary_parts.append(f"bookmaker={bookmaker_id}")
        else:
            filters_summary_parts.append(f"bookmakers={n_books} ({bookmaker_id})")
    filters_str = (
        f" [{', '.join(filters_summary_parts)}]" if filters_summary_parts else ""
    )
    await set_progress(
        f"{league.id} ({phase}): fetching events from SGO{filters_str}"
    )

    report = LeagueReport(league_id=league.id)
    event_idx = 0
    first_event_seen = False
    for payload in client.get_events(
        league_id=league.id,
        # include_open_close=False: we expose ``opening_odds`` in the schema
        # but no internal code actually reads it. Dropping the flag halves
        # the bookmaker_selection rows we have to upsert. Quota-neutral
        # (1 entity per event regardless of payload shape) but saves
        # bandwidth + DB writes. Flip back on only if a downstream
        # consumer starts reading historical opens/closes.
        include_open_close=False,
        # include_alt_lines: alt spreads/totals balloon the markets +
        # selections we'd persist (5–10× the row count) and we don't
        # render or grade them. Default off saves DB rows, NOT quota
        # (SGO still counts the event once regardless).
        include_alt_lines=settings.ingest_include_alt_lines,
        # odd_ids / bookmaker_id: response-shape filters. Cut DB write
        # time + storage when set — but they DO NOT reduce per-month
        # entity quota (SGO bills 1 per event). For real quota savings
        # use a smaller window or less frequent cadence.
        odd_ids=odd_ids,
        bookmaker_id=bookmaker_id,
        odds_available=None,
        starts_after=starts_after_iso,
        starts_before=starts_before_iso,
        limit=50,
    ):
        # First payload arriving means SGO returned the first page —
        # signal "fetch done, processing begins" so operators can
        # distinguish a slow upstream from a slow ingest.
        if not first_event_seen:
            first_event_seen = True
            await set_progress(
                f"{league.id}: SGO returned first batch — starting ingest"
            )
        # Per-event check too — leagues with many events can take long
        # enough that operators want sub-league responsiveness on Stop.
        await raise_if_cancelled()
        event_idx += 1
        event_id = payload.get("eventID") or "?"
        await set_progress(
            f"{league.id} event {event_idx} ({event_id}): writing odds"
        )
        try:
            result = await ingest_event(
                session, payload,
                skip_if_new_terminal=True,
                phase=phase,
            )
        except Exception:  # noqa: BLE001
            logger.exception("ingest failed for event=%s", payload.get("eventID"))
            await session.rollback()
            report.events_failed += 1
            await set_progress(
                f"{league.id} event {event_idx} ({event_id}): FAILED — see logs"
            )
            continue
        if result is not None:
            report.events_processed += 1
            report.transitions.append(result.transition)
            await set_progress(
                f"{league.id} event {event_idx} ({event_id}): committed "
                f"(written={result.selections_written} "
                f"graded={result.selections_graded} "
                f"transition={result.transition.name})"
            )
        else:
            await set_progress(
                f"{league.id} event {event_idx} ({event_id}): skipped"
            )
        # Commit per-event so markets/selections/quotes become visible
        # mid-walk. Each event is idempotent on retry, so a partial
        # league walk is recoverable on the next run.
        await session.commit()
    league.last_refreshed_at = datetime.now(tz=timezone.utc)
    logger.info(
        "ingest_league %s: %d events processed, %d failed",
        league.id, report.events_processed, report.events_failed,
    )
    await set_progress(
        f"{league.id}: finished — {report.events_processed} processed, "
        f"{report.events_failed} failed"
    )
    return report


async def ingest_due_leagues(
    session: AsyncSession,
    client: SgoClient,
    *,
    phase: str = "all",
) -> list[LeagueReport]:
    """Walk every active League in the DB and ingest each one. Cadence-gating
    is intentionally omitted for v1 — the ARQ cron decides scheduling. Inside
    a single call, every active league is touched once.

    ``phase`` forwards to ``ingest_league`` / ``ingest_event``. The default
    ``"all"`` matches historical behavior (used by ``full_refresh`` and the
    standalone ``ingest_due_leagues`` cron).
    """
    from sqlalchemy import select

    leagues = list(await session.scalars(
        select(League).where(League.active.is_(True))
    ))
    await set_progress(
        f"walking {len(leagues)} active league(s) [phase={phase}]"
    )
    reports: list[LeagueReport] = []
    for idx, league in enumerate(leagues, start=1):
        # Cooperative cancellation point — operator's Stop click takes
        # effect at the next league boundary (worst case: end of current
        # league's walk, typically 5–30s).
        await raise_if_cancelled()
        await set_progress(
            f"[{idx}/{len(leagues)}] starting {league.id}"
        )
        reports.append(
            await ingest_league(session, league, client, phase=phase)
        )
        # Commit per-league so a cancellation (or a single league's
        # failure) doesn't roll back leagues we've already finished.
        # Each league is self-contained — no cross-league constraints.
        await session.commit()
    await set_progress(
        f"all leagues done — {sum(r.events_processed for r in reports)} events processed, "
        f"{sum(r.events_failed for r in reports)} failed"
    )
    return reports
