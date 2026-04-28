"""``/v1/auth/*`` — register, login, refresh, logout, me."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from aggrigator.deps import SessionDep, SettingsDep, require_user
from aggrigator.models.auth import RefreshToken, User
from aggrigator.schemas.auth import (
    AccessOnly,
    LoginIn,
    RefreshIn,
    RegisterIn,
    TokenPair,
    UserOut,
)
from aggrigator.security import audit
from aggrigator.security.jwt import (
    InvalidToken,
    issue_access_token,
    issue_refresh_token,
    verify_refresh_token,
)
from aggrigator.security.passwords import (
    hash_password,
    needs_rehash,
    verify_password,
)


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _hash_refresh(token: str) -> str:
    """SHA-256 of the JWT — opaque, fast, and good enough for revocation lookup
    (we already gate by JWT signature; this is just to map an incoming refresh
    to its row)."""
    return hashlib.sha256(token.encode()).hexdigest()


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(
    request: Request, payload: RegisterIn, session: SessionDep,
) -> User:
    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")
    await session.refresh(user)
    await audit.write(
        session, event_name=audit.Event.AUTH_REGISTER,
        actor_user_id=user.id, ip=_ip(request),
    )
    await session.commit()
    return user


@router.post("/login", response_model=TokenPair)
async def login(
    request: Request, payload: LoginIn, session: SessionDep, settings: SettingsDep,
) -> TokenPair:
    user = await session.scalar(select(User).where(User.email == payload.email))
    if user is None or not user.is_active or not verify_password(
        payload.password, user.password_hash
    ):
        await audit.write(
            session, event_name=audit.Event.AUTH_LOGIN_FAIL,
            actor_user_id=user.id if user else None, ip=_ip(request),
            metadata={"email": payload.email},
        )
        await session.commit()
        # Identical 401 shape on bad email vs bad password — no enumeration.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    access = issue_access_token(
        user_id=user.id, role=user.role, tier=user.tier,
        secret=settings.jwt_secret, ttl_seconds=settings.jwt_access_ttl_seconds,
    )
    refresh, jti = issue_refresh_token(
        user_id=user.id,
        secret=settings.jwt_secret, ttl_seconds=settings.jwt_refresh_ttl_seconds,
    )
    session.add(RefreshToken(
        id=uuid.UUID(jti),
        user_id=user.id,
        token_hash=_hash_refresh(refresh),
        expires_at=datetime.now(tz=timezone.utc)
        + timedelta(seconds=settings.jwt_refresh_ttl_seconds),
    ))
    await audit.write(
        session, event_name=audit.Event.AUTH_LOGIN_OK,
        actor_user_id=user.id, ip=_ip(request),
    )
    await session.commit()
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.jwt_access_ttl_seconds,
    )


@router.post("/refresh", response_model=AccessOnly)
async def refresh(
    payload: RefreshIn, session: SessionDep, settings: SettingsDep
) -> AccessOnly:
    try:
        claims = verify_refresh_token(payload.refresh_token, settings.jwt_secret)
    except InvalidToken:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid refresh token")

    row = await session.get(RefreshToken, uuid.UUID(claims.jti))
    if (
        row is None
        or row.revoked_at is not None
        or row.token_hash != _hash_refresh(payload.refresh_token)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid refresh token")

    user = await session.get(User, uuid.UUID(claims.sub))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid refresh token")

    access = issue_access_token(
        user_id=user.id, role=user.role, tier=user.tier,
        secret=settings.jwt_secret, ttl_seconds=settings.jwt_access_ttl_seconds,
    )
    return AccessOnly(
        access_token=access, expires_in=settings.jwt_access_ttl_seconds
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: RefreshIn, session: SessionDep, settings: SettingsDep
) -> None:
    """Revokes the supplied refresh token. Access tokens stay valid until they
    expire — they're stateless. To kill an access token immediately, also
    revoke at the gateway layer (out of v1 scope)."""
    try:
        claims = verify_refresh_token(payload.refresh_token, settings.jwt_secret)
    except InvalidToken:
        return None
    row = await session.get(RefreshToken, uuid.UUID(claims.jti))
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(tz=timezone.utc)
        await session.commit()


@router.get("/me", response_model=UserOut)
async def me(user: Annotated[User, Depends(require_user)]) -> User:
    return user
