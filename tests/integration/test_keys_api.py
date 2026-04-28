"""End-to-end API-key management tests."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


async def _login(client) -> str:
    await client.post(
        "/v1/auth/register",
        json={"email": "keys@example.com", "password": "hunter2hunter2"},
    )
    r = await client.post(
        "/v1/auth/login",
        json={"email": "keys@example.com", "password": "hunter2hunter2"},
    )
    return r.json()["access_token"]


async def test_create_list_and_use_key(client) -> None:
    access = await _login(client)
    auth = {"Authorization": f"Bearer {access}"}

    # Create
    r = await client.post("/v1/keys", json={"name": "Production"}, headers=auth)
    assert r.status_code == 201, r.text
    body = r.json()
    raw_key = body["key"]
    assert raw_key.startswith("agg_")
    assert body["last_four"] == raw_key[-4:]

    # List — note the raw key is gone
    r = await client.get("/v1/keys", headers=auth)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert "key" not in rows[0]

    # Use the raw key on /me
    r = await client.get("/v1/auth/me", headers={"X-Api-Key": raw_key})
    assert r.status_code == 200
    assert r.json()["email"] == "keys@example.com"

    # Bearer Authorization should also accept the raw API key form.
    r = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {raw_key}"})
    assert r.status_code == 200


async def test_revoked_key_rejected(client) -> None:
    access = await _login(client)
    auth = {"Authorization": f"Bearer {access}"}

    create = await client.post("/v1/keys", json={"name": "to-revoke"}, headers=auth)
    raw = create.json()["key"]
    key_id = create.json()["id"]

    # Revoke
    r = await client.delete(f"/v1/keys/{key_id}", headers=auth)
    assert r.status_code == 204

    # Now using it should fail
    r = await client.get("/v1/auth/me", headers={"X-Api-Key": raw})
    assert r.status_code == 401


async def test_rotate_revokes_old_and_issues_new(client) -> None:
    access = await _login(client)
    auth = {"Authorization": f"Bearer {access}"}

    create = await client.post("/v1/keys", json={"name": "rot"}, headers=auth)
    old_raw = create.json()["key"]
    old_id = create.json()["id"]

    r = await client.post(f"/v1/keys/{old_id}/rotate", headers=auth)
    assert r.status_code == 200, r.text
    new_raw = r.json()["key"]
    assert new_raw != old_raw

    # Old key no longer authenticates
    r = await client.get("/v1/auth/me", headers={"X-Api-Key": old_raw})
    assert r.status_code == 401

    # New key works
    r = await client.get("/v1/auth/me", headers={"X-Api-Key": new_raw})
    assert r.status_code == 200


async def test_cannot_revoke_other_users_key(client) -> None:
    # User A creates a key
    await client.post(
        "/v1/auth/register", json={"email": "a@example.com", "password": "hunter2hunter2"}
    )
    a_login = await client.post(
        "/v1/auth/login", json={"email": "a@example.com", "password": "hunter2hunter2"}
    )
    a_auth = {"Authorization": f"Bearer {a_login.json()['access_token']}"}
    a_key = await client.post("/v1/keys", json={"name": "a-prod"}, headers=a_auth)
    a_key_id = a_key.json()["id"]

    # User B tries to delete it
    await client.post(
        "/v1/auth/register", json={"email": "b@example.com", "password": "hunter2hunter2"}
    )
    b_login = await client.post(
        "/v1/auth/login", json={"email": "b@example.com", "password": "hunter2hunter2"}
    )
    b_auth = {"Authorization": f"Bearer {b_login.json()['access_token']}"}

    r = await client.delete(f"/v1/keys/{a_key_id}", headers=b_auth)
    assert r.status_code == 404  # not 403 — never reveal cross-user existence


async def test_garbage_api_key_rejected(client) -> None:
    r = await client.get("/v1/auth/me", headers={"X-Api-Key": "agg_live_garbage"})
    assert r.status_code == 401
