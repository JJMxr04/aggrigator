"""Single-attempt webhook delivery + retry-policy decision.

Pure of the queue layer (ARQ): given a ``WebhookDelivery`` row and the raw
secret, ``send_one`` POSTs the payload, applies the §4.7 retry rules, and
mutates the row in place. Caller commits.

The httpx ``AsyncClient`` is injected so tests can swap a ``MockTransport``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from aggrigator.models import WebhookDelivery
from aggrigator.security.webhook_signing import HEADER_NAME, sign

logger = logging.getLogger(__name__)


# Plan §4.7. 8 attempts ≈ 8h cap.
RETRY_BACKOFF_SECONDS = [60, 120, 300, 900, 1800, 3600, 7200, 21600]
MAX_ATTEMPTS = len(RETRY_BACKOFF_SECONDS)

# 4xx codes that are still retryable (transient throttling / wait-y).
TRANSIENT_4XX = {408, 425, 429}


@dataclass(frozen=True)
class DeliveryOutcome:
    """The decision after one attempt — caller mutates the row from this."""
    success: bool
    permanent_fail: bool
    http_status: int | None
    error: str | None
    next_retry_at: datetime | None


async def send_one(
    delivery: WebhookDelivery,
    *,
    url: str,
    secret: str,
    client: httpx.AsyncClient,
    timeout_seconds: float = 10.0,
    now: datetime | None = None,
) -> DeliveryOutcome:
    """Make one POST attempt, mutate ``delivery`` with the result, return
    outcome."""
    body = json.dumps(delivery.payload, separators=(",", ":")).encode()
    header = sign(secret=secret, body=body)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "aggrigator/1.0",
        "X-Aggrigator-Event-Id": delivery.event_id,
        "X-Aggrigator-Event-Name": delivery.event_name,
        "X-Aggrigator-Delivery-Id": str(delivery.id),
        "X-Aggrigator-Idempotency-Key": delivery.idempotency_key,
        "X-Aggrigator-Schema-Version": str(delivery.payload.get("schema_version", 1)),
        HEADER_NAME: str(header),
    }

    moment = now or datetime.now(tz=timezone.utc)
    delivery.attempts += 1
    delivery.last_attempt_at = moment

    try:
        resp = await client.post(url, content=body, headers=headers, timeout=timeout_seconds)
    except httpx.HTTPError as exc:
        return _decide_retry(delivery, status=None, error=str(exc), now=moment)

    delivery.last_status = resp.status_code
    if 200 <= resp.status_code < 300 or resp.status_code == 409:
        # 409 = receiver already processed this idempotency_key (treat as success).
        delivery.delivered_at = moment
        delivery.next_retry_at = None
        delivery.last_error = None
        return DeliveryOutcome(
            success=True, permanent_fail=False,
            http_status=resp.status_code, error=None, next_retry_at=None,
        )

    if resp.status_code in TRANSIENT_4XX or resp.status_code >= 500:
        return _decide_retry(
            delivery, status=resp.status_code,
            error=resp.text[:500], now=moment,
        )

    # Permanent client error — don't retry.
    delivery.last_error = resp.text[:500]
    delivery.next_retry_at = None
    return DeliveryOutcome(
        success=False, permanent_fail=True,
        http_status=resp.status_code, error=delivery.last_error, next_retry_at=None,
    )


def _decide_retry(
    delivery: WebhookDelivery,
    *,
    status: int | None,
    error: str,
    now: datetime,
) -> DeliveryOutcome:
    """Set next_retry_at per backoff, or give up if attempts exhausted."""
    delivery.last_error = error
    if delivery.attempts >= MAX_ATTEMPTS:
        delivery.next_retry_at = None
        return DeliveryOutcome(
            success=False, permanent_fail=True,
            http_status=status, error=error, next_retry_at=None,
        )
    backoff = RETRY_BACKOFF_SECONDS[delivery.attempts - 1]
    next_at = now + timedelta(seconds=backoff)
    delivery.next_retry_at = next_at
    return DeliveryOutcome(
        success=False, permanent_fail=False,
        http_status=status, error=error, next_retry_at=next_at,
    )
