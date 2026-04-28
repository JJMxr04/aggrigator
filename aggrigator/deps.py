"""FastAPI dependencies — DB sessions, current user, current API key.

The current-user / current-api-key deps return ``None`` on failure rather than
raising; the route handler decides whether anonymous access is acceptable. The
``require_*`` variants raise ``HTTPException(401)``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.config import Settings, get_settings
from aggrigator.db import get_session
from aggrigator.models.auth import ApiKey, User
from aggrigator.security.api_keys import split_raw, verify_key
from aggrigator.security.jwt import InvalidToken, verify_access_token


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _bearer_or_apikey(authorization: str | None, x_api_key: str | None) -> tuple[str, str] | None:
    """Returns ``("bearer", token)`` | ``("apikey", token)`` | ``None``."""
    if x_api_key:
        return "apikey", x_api_key
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            # An ``agg_*`` looks like an API key even when sent as Bearer.
            if value.startswith("agg_"):
                return "apikey", value
            return "bearer", value
    return None


async def current_user(
    session: SessionDep,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
) -> User | None:
    """Returns the authenticated User, or None if no/invalid credentials."""
    creds = _bearer_or_apikey(authorization, x_api_key)
    if creds is None:
        return None
    kind, token = creds

    if kind == "bearer":
        try:
            claims = verify_access_token(token, settings.jwt_secret)
        except InvalidToken:
            return None
        user_id = uuid.UUID(claims.sub)
        user = await session.get(User, user_id)
        return user if user and user.is_active else None

    # API key path
    user, _ = await _resolve_api_key(session, token)
    return user


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


async def current_api_key(
    request: Request,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
) -> ApiKey | None:
    creds = _bearer_or_apikey(authorization, x_api_key)
    if creds is None or creds[0] != "apikey":
        return None
    _, api_key = await _resolve_api_key(session, creds[1])
    if api_key is not None:
        api_key.last_used_at = datetime.now(tz=timezone.utc)
        api_key.last_used_ip = request.client.host if request.client else None
        await session.commit()
    return api_key


async def _resolve_api_key(
    session: AsyncSession, raw: str
) -> tuple[User | None, ApiKey | None]:
    parts = split_raw(raw)
    if parts is None:
        return None, None
    prefix, _ = parts
    row = await session.scalar(
        select(ApiKey).where(ApiKey.prefix == prefix, ApiKey.revoked_at.is_(None))
    )
    if row is None:
        return None, None
    if not verify_key(raw, expected_prefix=row.prefix, key_hash=row.key_hash):
        return None, None
    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        return None, None
    return user, row
