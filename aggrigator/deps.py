"""FastAPI dependencies — DB sessions, current user.

``current_user`` returns ``None`` on failure rather than raising; the route
handler decides whether anonymous access is acceptable. The ``require_*``
variants raise ``HTTPException(401)``.

Only the JWT path remains: aggrigator is single-tenant (MDProject only) and
the multi-tenant API-key system has been removed.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.config import Settings, get_settings
from aggrigator.db import get_session
from aggrigator.models.auth import User
from aggrigator.models.tenant import TenantApiKey, TenantStatus, TenantTier, TenantUser
from aggrigator.security.api_keys import split_raw, verify_key
from aggrigator.security.jwt import InvalidToken, verify_access_token
from aggrigator.security.webhook_signing import InvalidSignature, verify

logger = logging.getLogger(__name__)


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value
    return None


async def current_user(
    session: SessionDep,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> User | None:
    """Returns the authenticated User, or None if no/invalid credentials."""
    token = _bearer(authorization)
    if token is None:
        return None
    try:
        claims = verify_access_token(token, settings.jwt_secret)
    except InvalidToken:
        return None
    user_id = uuid.UUID(claims.sub)
    user = await session.get(User, user_id)
    return user if user and user.is_active else None


async def require_user(user: Annotated[User | None, Depends(current_user)]) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def require_admin(user: Annotated[User, Depends(require_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")
    return user


# --- Paradise channel: MDProject → aggrigator internal API ----------------


async def require_paradise_signed(
    request: Request,
    settings: SettingsDep,
) -> None:
    """HMAC-verify the request body against ``X-Paradise-Signature``.

    Used by every ``/v1/internal/*`` route. 503 (not 401) if the secret
    isn't configured — that's an operator misconfiguration, not a caller
    fault. 401 on signature mismatch / stale timestamp / missing header.
    """
    if not settings.paradise_secret:
        logger.error(
            "AGG_PARADISE_SECRET unset — every /v1/internal/* call will 503. "
            "Set it to the same value as MDProject's PARADISE_SECRET."
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="paradise channel not configured",
        )
    body = await request.body()
    header = request.headers.get("X-Paradise-Signature", "")
    try:
        verify(secret=settings.paradise_secret, body=body, header_value=header)
    except InvalidSignature as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"bad signature: {exc}",
        )


# --- Tenant gate: caller is a paid (PRO) user with a live API key ----------


_ENTITLED_STATUSES = (
    TenantStatus.TRIALING,
    TenantStatus.ACTIVE,
    TenantStatus.PAST_DUE,  # grace window — keep access while Stripe retries
)


async def require_pro_user(
    session: SessionDep,
    x_aggrigator_tenant_key: Annotated[str | None, Header()] = None,
) -> TenantUser:
    """Read ``X-Aggrigator-Tenant-Key`` → ``TenantApiKey`` → ``TenantUser``,
    return the user iff their tier+status grants analytics access.

    Layered checks:
    1. Header present + parsable (``agg_{env}_{head5}{tail}``).
    2. Prefix → row lookup (O(1) via unique index).
    3. argon2 verify of the secret tail.
    4. Key not revoked.
    5. User not revoked.
    6. Tier == PRO and status in (trialing | active | past_due).

    Failures collapse to 401/403 without leaking which step failed — a
    timing oracle on which-error-message would let an attacker enumerate
    valid prefixes.
    """
    if not x_aggrigator_tenant_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing tenant key",
        )

    parsed = split_raw(x_aggrigator_tenant_key)
    if parsed is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid tenant key",
        )
    prefix, _tail = parsed

    api_key = await session.scalar(
        select(TenantApiKey).where(TenantApiKey.prefix == prefix)
    )
    if api_key is None or api_key.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid tenant key",
        )

    if not verify_key(
        x_aggrigator_tenant_key,
        expected_prefix=api_key.prefix,
        key_hash=api_key.key_hash,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid tenant key",
        )

    tenant_user = await session.get(TenantUser, api_key.tenant_user_id)
    if tenant_user is None or tenant_user.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid tenant key",
        )

    if (
        tenant_user.tier != TenantTier.PRO
        or tenant_user.status not in _ENTITLED_STATUSES
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="PRO subscription required",
        )

    return tenant_user
