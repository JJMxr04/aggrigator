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
from aggrigator.ingest.lifecycle import Transition
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

    if not get_settings().webhook_target_url:
        # No receiver configured — skip enqueue rather than queue rows that
        # will never be dispatched. Log once per call so an operator can
        # diagnose silently-dropped deliveries.
        logger.warning(
            "Skipping webhook enqueue for event=%s: AGG_WEBHOOK_TARGET_URL "
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
