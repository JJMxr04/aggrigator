"""Enqueue WebhookDelivery rows for matching subscribed endpoints.

Called by the orchestrator after a successful ingest pass when the lifecycle
predicate returns a non-NONE transition. One row per (endpoint × event-state)
— deduplicated on ``idempotency_key`` so a re-ingest of unchanged state never
double-fires.
"""

from __future__ import annotations

import logging
import uuid
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.ingest.lifecycle import Transition
from aggrigator.models import Event, Selection, WebhookDelivery, WebhookEndpoint
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
    """Insert one ``WebhookDelivery`` row per matching enabled endpoint.

    Idempotency is enforced by a unique ``(endpoint_id, idempotency_key)``
    constraint plus an ``ON CONFLICT DO NOTHING`` insert — re-running this on
    unchanged event state is a no-op.

    Returns the list of *newly inserted* deliveries.
    """
    if transition == Transition.NONE:
        return []

    endpoints = await _matching_endpoints(session, transition.value)
    if not endpoints:
        return []

    selection_states = await _load_selection_states(session, event.id)
    blob = state_blob_from_event(
        status_type=event.status_type,
        home_score=event.home_score,
        away_score=event.away_score,
        selection_states=selection_states,
    )
    idempotency_key = _idempotency_key(event.id, blob)

    inserted: list[WebhookDelivery] = []
    for endpoint in endpoints:
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
                endpoint_id=endpoint.id,
                event_id=event.id,
                event_name=transition.value,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=["endpoint_id", "idempotency_key"])
            .returning(WebhookDelivery.id)
        )
        new_id = result.scalar_one_or_none()
        if new_id is not None:
            row = await session.get(WebhookDelivery, new_id)
            if row is not None:
                inserted.append(row)
    if inserted:
        logger.info(
            "Enqueued %d webhook delivery(ies) for event=%s transition=%s",
            len(inserted), event.id, transition.value,
        )
    return inserted


# ---- internals -------------------------------------------------------------


async def _matching_endpoints(
    session: AsyncSession, event_name: str
) -> list[WebhookEndpoint]:
    """Endpoints that are enabled, not revoked, and whose ``events`` array
    includes this event_name (Postgres ``ANY``)."""
    rows = await session.scalars(
        select(WebhookEndpoint).where(
            WebhookEndpoint.enabled.is_(True),
            WebhookEndpoint.events.any(event_name),
        )
    )
    return list(rows)


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
