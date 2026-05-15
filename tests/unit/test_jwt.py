"""Unit tests for security.jwt — issue/verify access + refresh."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt as jose_jwt

from aggrigator.security.jwt import (
    AccessClaims,
    ALGORITHM,
    AUDIENCE,
    ISSUER,
    InvalidToken,
    RefreshClaims,
    issue_access_token,
    issue_refresh_token,
    verify_access_token,
    verify_refresh_token,
)

SECRET = "test-secret-32-bytes-of-entropy-please"
USER_ID = uuid.uuid4()


def test_access_round_trip() -> None:
    token = issue_access_token(
        user_id=USER_ID, role="user",
        secret=SECRET, ttl_seconds=60,
    )
    claims = verify_access_token(token, SECRET)
    assert isinstance(claims, AccessClaims)
    assert claims.sub == str(USER_ID)
    assert claims.role == "user"


def test_access_carries_role() -> None:
    token = issue_access_token(
        user_id=USER_ID, role="admin",
        secret=SECRET, ttl_seconds=60,
    )
    claims = verify_access_token(token, SECRET)
    assert claims.role == "admin"


def test_refresh_round_trip_returns_jti() -> None:
    token, jti = issue_refresh_token(
        user_id=USER_ID, secret=SECRET, ttl_seconds=3600
    )
    claims = verify_refresh_token(token, SECRET)
    assert isinstance(claims, RefreshClaims)
    assert claims.jti == jti
    assert claims.sub == str(USER_ID)


def test_access_token_rejects_wrong_secret() -> None:
    token = issue_access_token(
        user_id=USER_ID, role="user",
        secret=SECRET, ttl_seconds=60,
    )
    with pytest.raises(InvalidToken):
        verify_access_token(token, "different-secret")


def test_access_token_rejects_expired() -> None:
    past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    token = issue_access_token(
        user_id=USER_ID, role="user",
        secret=SECRET, ttl_seconds=60, now=past,
    )
    with pytest.raises(InvalidToken):
        verify_access_token(token, SECRET)


def test_access_verify_rejects_refresh_token() -> None:
    token, _ = issue_refresh_token(
        user_id=USER_ID, secret=SECRET, ttl_seconds=3600
    )
    with pytest.raises(InvalidToken):
        verify_access_token(token, SECRET)


def test_refresh_verify_rejects_access_token() -> None:
    token = issue_access_token(
        user_id=USER_ID, role="user",
        secret=SECRET, ttl_seconds=60,
    )
    with pytest.raises(InvalidToken):
        verify_refresh_token(token, SECRET)


def test_access_rejects_wrong_audience() -> None:
    """Hand-roll a token with the wrong ``aud`` claim and confirm we reject."""
    payload = {
        "iss": ISSUER,
        "aud": "some-other-app",
        "sub": str(USER_ID),
        "type": "access",
        "iat": int(datetime.now(tz=timezone.utc).timestamp()),
        "exp": int((datetime.now(tz=timezone.utc) + timedelta(minutes=5)).timestamp()),
    }
    token = jose_jwt.encode(payload, SECRET, algorithm=ALGORITHM)
    with pytest.raises(InvalidToken):
        verify_access_token(token, SECRET)


def test_access_rejects_wrong_issuer() -> None:
    payload = {
        "iss": "imposter",
        "aud": AUDIENCE,
        "sub": str(USER_ID),
        "type": "access",
        "iat": int(datetime.now(tz=timezone.utc).timestamp()),
        "exp": int((datetime.now(tz=timezone.utc) + timedelta(minutes=5)).timestamp()),
    }
    token = jose_jwt.encode(payload, SECRET, algorithm=ALGORITHM)
    with pytest.raises(InvalidToken):
        verify_access_token(token, SECRET)


def test_two_refresh_tokens_have_distinct_jti() -> None:
    _, jti1 = issue_refresh_token(user_id=USER_ID, secret=SECRET, ttl_seconds=3600)
    _, jti2 = issue_refresh_token(user_id=USER_ID, secret=SECRET, ttl_seconds=3600)
    assert jti1 != jti2
