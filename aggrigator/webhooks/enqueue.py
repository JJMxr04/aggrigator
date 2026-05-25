"""Enqueue WebhookDelivery rows for the (single) MDProject receiver.

Called by the orchestrator after a successful ingest pass when the lifecycle
predicate returns a non-NONE transition. One row per event-state — deduplicated
on ``idempotency_key`` so a re-ingest of unchanged state never double-fires.
"""

from __future__ import annotations

import logging
import uuid
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.config import get_settings
from aggrigator.ingest.lifecycle import EventState, Transition, decide_transition
from aggrigator.models import Event, Selection, WebhookDelivery
from aggrigator.webhooks.idempotency import (
    idempotency_key as _idempotency_key,
    state_blob_from_event,
)
from aggrigator.webhooks.payload import build_payload

logger = logging.getLogger(__name__)


async def enqueue_for_event(
    session: AsyncSession,
    event: Event,
    transition: Transition,
) -> list[WebhookDelivery]:
    """Insert a ``WebhookDelivery`` row for this event-state transition.

    Idempotency is enforced by a unique ``idempotency_key`` constraint plus an
    ``ON CONFLICT DO NOTHING`` insert — re-running this on unchanged event
    state is a no-op.

    Returns the list of *newly inserted* deliveries (zero or one).
    """
    if transition == Transition.NONE:
        return []

    # MDProject's receiver only runs business logic for finalized + voided
    # (settle / reopen). LIFECYCLE_CHANGED would just upsert Event/Market
    # rows the portal already reads through-the-aggregator, so we don't
    # bother enqueuing — saves a delivery + a receiver-side write per
    # ``notstarted → inprogress`` flip on every live event. The classifier
    # still returns LIFECYCLE_CHANGED for reports / logs.
    if transition == Transition.LIFECYCLE_CHANGED:
        logger.debug(
            "Skipping webhook enqueue for event=%s: LIFECYCLE_CHANGED "
            "transitions are filtered out (only FINALIZED + VOIDED fire).",
            event.id,
        )
        return []

    if not get_settings().webhook_target_url:
        # No receiver configured — skip enqueue rather than queue rows that
        # will never be dispatched. Log once per call so an operator can
        # diagnose silently-dropped deliveries.
        logger.warning(
            "Skipping webhook enqueue for event=%s: AGG_MDPROJECT_URL "
            "is unset.", event.id,
        )
        return []

    selection_states = await _load_selection_states(session, event.id)
    blob = state_blob_from_event(
        status_type=event.status_type,
        home_score=event.home_score,
        away_score=event.away_score,
        selection_states=selection_states,
    )
    idempotency_key = _idempotency_key(event.id, blob)

    delivery_id = uuid.uuid4()
    payload = await build_payload(
        session, event,
        event_name=transition.value,
        delivery_id=delivery_id,
        idempotency_key=idempotency_key,
    )
    result = await session.execute(
        insert(WebhookDelivery)
        .values(
            id=delivery_id,
            event_id=event.id,
            event_name=transition.value,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(WebhookDelivery.id)
    )
    new_id = result.scalar_one_or_none()
    if new_id is None:
        return []
    row = await session.get(WebhookDelivery, new_id)
    if row is None:
        return []
    logger.info(
        "Enqueued webhook delivery for event=%s transition=%s",
        event.id, transition.value,
    )
    return [row]


async def force_enqueue_for_event(
    session: AsyncSession,
    event: Event,
) -> WebhookDelivery | None:
    """Manually enqueue a delivery for ``event`` as if a fresh change was just
    detected. Used by the admin "Re-enqueue webhook" action for ops recovery.

    Three differences from ``enqueue_for_event``:

    1. Picks the transition from the event's *current* state — same call the
       orchestrator would make on first observation (``previous=None``).
    2. Generates a unique ``idempotency_key`` with a ``:manual:<short-uuid>``
       suffix so MDProject's ``WebhookReceived`` table doesn't 409-dedup
       this delivery against the original.
    3. Always inserts — no ``ON CONFLICT DO NOTHING`` guard, since the
       operator explicitly asked for a re-send.

    Caller commits and triggers ``notify_webhook_worker`` separately.
    """
    if not get_settings().webhook_target_url:
        logger.warning(
            "Skipping force-enqueue for event=%s: AGG_MDPROJECT_URL is unset.",
            event.id,
        )
        return None

    # decide_transition(previous=None, current) → FINALIZED for finished,
    # VOIDED for postponed/canceled, LIFECYCLE_CHANGED otherwise. Mirrors
    # what the orchestrator would have fired the first time it saw this
    # state — exactly the semantics of "re-detect this event".
    current = EventState(
        status_type=event.status_type,
        home_score=event.home_score,
        away_score=event.away_score,
    )
    transition = decide_transition(None, current)
    # Mirror the LIFECYCLE_CHANGED filter from ``enqueue_for_event``: only
    # finalized + voided events have receiver-side business logic, so the
    # manual button shouldn't fabricate a delivery for an in-progress
    # event either. The admin action surfaces this as "skipped — not in a
    # terminal state" rather than silently returning None.
    if transition == Transition.LIFECYCLE_CHANGED:
        logger.info(
            "Skipping force-enqueue for event=%s: status_type=%s is not a "
            "terminal state (only finished/postponed/canceled fire webhooks).",
            event.id, event.status_type,
        )
        return None

    selection_states = await _load_selection_states(session, event.id)
    blob = state_blob_from_event(
        status_type=event.status_type,
        home_score=event.home_score,
        away_score=event.away_score,
        selection_states=selection_states,
    )
    base_key = _idempotency_key(event.id, blob)
    idempotency_key = f"{base_key}:manual:{uuid.uuid4().hex[:8]}"

    delivery_id = uuid.uuid4()
    payload = await build_payload(
        session, event,
        event_name=transition.value,
        delivery_id=delivery_id,
        idempotency_key=idempotency_key,
    )
    row = WebhookDelivery(
        id=delivery_id,
        event_id=event.id,
        event_name=transition.value,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    session.add(row)
    await session.flush()
    logger.info(
        "Force-enqueued webhook delivery for event=%s transition=%s key=%s",
        event.id, transition.value, idempotency_key,
    )
    return row


# ---- internals -------------------------------------------------------------


async def _load_selection_states(
    session: AsyncSession, event_id: str
) -> Iterable[tuple[str, str]]:
    from aggrigator.models import Market  # local — avoid circular at module top
    rows = await session.execute(
        select(Selection.id, Selection.settlement_status)
        .join(Market, Selection.market_id == Market.id)
        .where(Market.event_id == event_id)
    )
    return [(sid, status) for sid, status in rows.all()]
