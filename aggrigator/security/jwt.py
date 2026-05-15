"""JWT issuance and verification.

HS256 with ``iss=aggrigator`` and ``aud=portal``. Access tokens carry
``{sub: user_id, role, type: "access"}``; refresh tokens carry
``{sub: user_id, type: "refresh", jti: <uuid>}``. ``jti`` lets the server
revoke a specific refresh token by hashing it into ``auth_refresh_token``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from jose import JWTError, jwt

ISSUER = "aggrigator"
AUDIENCE = "portal"
ALGORITHM = "HS256"


class InvalidToken(Exception):
    """Token is malformed, expired, has wrong issuer/audience, or signature invalid."""


@dataclass(frozen=True)
class AccessClaims:
    sub: str          # user_id (string UUID)
    role: str
    iat: datetime
    exp: datetime


@dataclass(frozen=True)
class RefreshClaims:
    sub: str
    jti: str
    iat: datetime
    exp: datetime


def issue_access_token(
    *,
    user_id: uuid.UUID | str,
    role: str,
    secret: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> str:
    issued_at = now or datetime.now(tz=timezone.utc)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def issue_refresh_token(
    *,
    user_id: uuid.UUID | str,
    secret: str,
    ttl_seconds: int,
    now: datetime | None = None,
    jti: str | None = None,
) -> tuple[str, str]:
    """Returns ``(token, jti)``. Persist a hash of ``token`` keyed on ``jti``."""
    issued_at = now or datetime.now(tz=timezone.utc)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    jti_str = jti or str(uuid.uuid4())
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(user_id),
        "type": "refresh",
        "jti": jti_str,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, secret, algorithm=ALGORITHM)
    return token, jti_str


def _decode(token: str, secret: str, expected_type: Literal["access", "refresh"]) -> dict:
    try:
        payload = jwt.decode(
            token, secret, algorithms=[ALGORITHM],
            audience=AUDIENCE, issuer=ISSUER,
            options={"require": ["sub", "iss", "aud", "exp", "type"]},
        )
    except JWTError as exc:
        raise InvalidToken(str(exc)) from exc
    if payload.get("type") != expected_type:
        raise InvalidToken(f"expected type={expected_type}, got {payload.get('type')!r}")
    return payload


def verify_access_token(token: str, secret: str) -> AccessClaims:
    payload = _decode(token, secret, "access")
    try:
        return AccessClaims(
            sub=payload["sub"],
            role=payload.get("role", "user"),
            iat=datetime.fromtimestamp(payload["iat"], tz=timezone.utc),
            exp=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidToken(f"missing claim: {exc}") from exc


def verify_refresh_token(token: str, secret: str) -> RefreshClaims:
    payload = _decode(token, secret, "refresh")
    try:
        return RefreshClaims(
            sub=payload["sub"],
            jti=payload["jti"],
            iat=datetime.fromtimestamp(payload["iat"], tz=timezone.utc),
            exp=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidToken(f"missing claim: {exc}") from exc
