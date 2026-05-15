"""FastAPI dependencies — DB sessions, current user.

``current_user`` returns ``None`` on failure rather than raising; the route
handler decides whether anonymous access is acceptable. The ``require_*``
variants raise ``HTTPException(401)``.

Only the JWT path remains: aggrigator is single-tenant (MDProject only) and
the multi-tenant API-key system has been removed.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.config import Settings, get_settings
from aggrigator.db import get_session
from aggrigator.models.auth import User
from aggrigator.security.jwt import InvalidToken, verify_access_token


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
