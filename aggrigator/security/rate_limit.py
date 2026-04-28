"""Rate-limiting wiring.

slowapi is the underlying engine. The plan §8 ladder:

- ``auth-anon``        — 10/min/IP
- ``auth-user`` free   — 120/min/user_id
- ``auth-user`` pro    — 600/min/user_id
- ``auth-user`` enterprise / service — 6000/min/user_id (first-party)
- ``service-key`` standard — 600/min/api_key
- ``service-key`` service  — 6000/min/api_key
- per-IP backstop — 3000/min on every authenticated route

The "first-party promotion" comes from ``X-Client-App`` header lookup against
the ``client_app`` table — if the slug is enabled+trusted+service-tier we
treat the request as service-tier regardless of the end-user's tier. Looked
up at request time; cached on ``request.state`` for downstream deps.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from fastapi import HTTPException, Request, status
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import select

from aggrigator.config import get_settings
from aggrigator.db import async_session_factory
from aggrigator.models.auth import ClientApp, UserTier

logger = logging.getLogger(__name__)


# Steady-state rates per the plan (§8). These are slowapi-style strings.
TIER_RATE: dict[str, str] = {
    UserTier.SERVICE: "6000/minute",
    UserTier.ENTERPRISE: "3000/minute",
    UserTier.PRO: "600/minute",
    UserTier.FREE: "120/minute",
}
ANON_RATE = "10/minute"
PER_IP_BACKSTOP_RATE = "3000/minute"


def _key_func_with_app(request: Request) -> str:
    """Identifies the rate-limit bucket for the current request.

    Prefers (in order): API-key id, user id (from JWT), promoted client_app
    slug, raw IP. The bucket key is augmented by the chosen tier so two
    requests from the same user under different ``X-Client-App`` slugs land
    in different buckets — preventing a free-tier user from sneaking past
    by spoofing the header (the *actual* tier still comes from the
    ``client_app`` table).
    """
    state = getattr(request, "state", None)
    api_key = getattr(state, "api_key", None) if state else None
    user = getattr(state, "user", None) if state else None
    tier = getattr(state, "effective_tier", None) if state else None
    if api_key is not None:
        return f"key:{api_key.id}:{tier}"
    if user is not None:
        return f"user:{user.id}:{tier}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=_key_func_with_app)


async def resolve_first_party_promotion(
    request: Request,
    call_next: Callable[[Request], Awaitable],
):
    """ASGI middleware — looks up ``X-Client-App`` and stamps tier on
    ``request.state`` for the limiter to see.

    Also enforces the per-IP backstop on every authenticated route via
    ``request.state.effective_tier`` defaulting to None (anon).
    """
    request.state.client_app = None
    request.state.effective_tier = None

    slug = request.headers.get("X-Client-App")
    if slug:
        async with async_session_factory() as session:
            row = await session.scalar(
                select(ClientApp).where(
                    ClientApp.slug == slug,
                    ClientApp.revoked_at.is_(None),
                )
            )
        if row is not None and row.trusted and row.tier == UserTier.SERVICE:
            request.state.client_app = row
            request.state.effective_tier = UserTier.SERVICE

    response = await call_next(request)
    return response


async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """slowapi -> 429 JSON instead of HTML."""
    logger.warning(
        "rate limit hit on %s key=%s",
        request.url.path, _key_func_with_app(request),
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="rate limit exceeded",
    )
