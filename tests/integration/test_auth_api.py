"""End-to-end auth tests against a real Postgres."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


async def test_register_login_me_round_trip(client) -> None:
    r = await client.post(
        "/v1/auth/register",
        json={"email": "alice@example.com", "password": "hunter2hunter2"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["email"] == "alice@example.com"

    r = await client.post(
        "/v1/auth/login",
        json={"email": "alice@example.com", "password": "hunter2hunter2"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    access = body["access_token"]
    assert body["expires_in"] > 0

    r = await client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {access}"}
    )
    assert r.status_code == 200
    assert r.json()["email"] == "alice@example.com"
    assert r.json()["tier"] == "free"


async def test_register_duplicate_email_409(client) -> None:
    payload = {"email": "dup@example.com", "password": "hunter2hunter2"}
    assert (await client.post("/v1/auth/register", json=payload)).status_code == 201
    r = await client.post("/v1/auth/register", json=payload)
    assert r.status_code == 409


async def test_login_wrong_password_401(client) -> None:
    await client.post(
        "/v1/auth/register",
        json={"email": "bob@example.com", "password": "rightpassword"},
    )
    r = await client.post(
        "/v1/auth/login",
        json={"email": "bob@example.com", "password": "wrongpassword"},
    )
    assert r.status_code == 401


async def test_login_unknown_email_returns_same_401_shape(client) -> None:
    r1 = await client.post(
        "/v1/auth/login",
        json={"email": "missing@example.com", "password": "hunter2hunter2"},
    )
    await client.post(
        "/v1/auth/register",
        json={"email": "exists@example.com", "password": "rightpassword"},
    )
    r2 = await client.post(
        "/v1/auth/login",
        json={"email": "exists@example.com", "password": "wrongpassword"},
    )
    assert r1.status_code == 401
    assert r2.status_code == 401
    # Body shape identical — no enumeration vector.
    assert r1.json() == r2.json()


async def test_refresh_round_trip(client) -> None:
    await client.post(
        "/v1/auth/register",
        json={"email": "carol@example.com", "password": "hunter2hunter2"},
    )
    login = await client.post(
        "/v1/auth/login",
        json={"email": "carol@example.com", "password": "hunter2hunter2"},
    )
    refresh = login.json()["refresh_token"]

    r = await client.post("/v1/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 200
    new_access = r.json()["access_token"]
    me = await client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {new_access}"}
    )
    assert me.status_code == 200


async def test_logout_revokes_refresh_token(client) -> None:
    await client.post(
        "/v1/auth/register",
        json={"email": "dave@example.com", "password": "hunter2hunter2"},
    )
    login = await client.post(
        "/v1/auth/login",
        json={"email": "dave@example.com", "password": "hunter2hunter2"},
    )
    refresh = login.json()["refresh_token"]

    out = await client.post("/v1/auth/logout", json={"refresh_token": refresh})
    assert out.status_code == 204

    r = await client.post("/v1/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 401


async def test_me_without_auth_401(client) -> None:
    r = await client.get("/v1/auth/me")
    assert r.status_code == 401


async def test_me_with_invalid_token_401(client) -> None:
    r = await client.get(
        "/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert r.status_code == 401
