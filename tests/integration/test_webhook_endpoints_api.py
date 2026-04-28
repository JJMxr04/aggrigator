"""Tests for /v1/webhook-endpoints CRUD + delivery list."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from aggrigator.config import get_settings
from aggrigator.models import WebhookEndpoint
from aggrigator.security.webhook_signing import decrypt_secret
from tests.integration.factories import login_and_get_token

pytestmark = pytest.mark.asyncio


async def test_list_endpoints_requires_auth(client) -> None:
    r = await client.get("/v1/webhook-endpoints")
    assert r.status_code == 401


async def test_create_endpoint_returns_secret_once(client, session) -> None:
    token = await login_and_get_token(client)
    r = await client.post(
        "/v1/webhook-endpoints",
        json={
            "url": "https://example.com/sportgameodds/webhook",
            "description": "MDProject",
            "events": ["event.finalized", "event.voided"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["signing_secret"]
    assert body["url"].rstrip("/") == "https://example.com/sportgameodds/webhook"
    assert body["events"] == ["event.finalized", "event.voided"]

    # The secret in the DB is encrypted, not the plaintext.
    row = await session.scalar(select(WebhookEndpoint))
    assert row is not None
    assert row.secret_ciphertext != body["signing_secret"]
    settings = get_settings()
    assert decrypt_secret(
        row.secret_ciphertext, key=settings.secret_encryption_key
    ) == body["signing_secret"]


async def test_list_after_create(client) -> None:
    token = await login_and_get_token(client)
    auth = {"Authorization": f"Bearer {token}"}
    await client.post(
        "/v1/webhook-endpoints",
        json={"url": "https://example.com/hook", "events": ["event.finalized"]},
        headers=auth,
    )
    r = await client.get("/v1/webhook-endpoints", headers=auth)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert "signing_secret" not in rows[0]


async def test_patch_disable(client) -> None:
    token = await login_and_get_token(client)
    auth = {"Authorization": f"Bearer {token}"}
    create = await client.post(
        "/v1/webhook-endpoints",
        json={"url": "https://example.com/hook", "events": ["event.finalized"]},
        headers=auth,
    )
    eid = create.json()["id"]
    r = await client.patch(
        f"/v1/webhook-endpoints/{eid}",
        json={"enabled": False},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False


async def test_rotate_changes_secret(client, session) -> None:
    token = await login_and_get_token(client)
    auth = {"Authorization": f"Bearer {token}"}
    create = await client.post(
        "/v1/webhook-endpoints",
        json={"url": "https://example.com/hook", "events": ["event.finalized"]},
        headers=auth,
    )
    eid = create.json()["id"]
    first_secret = create.json()["signing_secret"]

    r = await client.post(f"/v1/webhook-endpoints/{eid}/rotate", headers=auth)
    assert r.status_code == 200
    second_secret = r.json()["signing_secret"]
    assert second_secret != first_secret


async def test_delete_endpoint(client) -> None:
    token = await login_and_get_token(client)
    auth = {"Authorization": f"Bearer {token}"}
    create = await client.post(
        "/v1/webhook-endpoints",
        json={"url": "https://example.com/hook", "events": ["event.finalized"]},
        headers=auth,
    )
    eid = create.json()["id"]
    r = await client.delete(f"/v1/webhook-endpoints/{eid}", headers=auth)
    assert r.status_code == 204
    r = await client.get("/v1/webhook-endpoints", headers=auth)
    assert r.json() == []


async def test_cannot_modify_other_users_endpoint(client) -> None:
    a_token = await login_and_get_token(client, email="a@example.com")
    create = await client.post(
        "/v1/webhook-endpoints",
        json={"url": "https://example.com/hook", "events": ["event.finalized"]},
        headers={"Authorization": f"Bearer {a_token}"},
    )
    eid = create.json()["id"]

    b_token = await login_and_get_token(client, email="b@example.com")
    r = await client.delete(
        f"/v1/webhook-endpoints/{eid}",
        headers={"Authorization": f"Bearer {b_token}"},
    )
    assert r.status_code == 404


async def test_deliveries_endpoint_empty(client) -> None:
    token = await login_and_get_token(client)
    auth = {"Authorization": f"Bearer {token}"}
    create = await client.post(
        "/v1/webhook-endpoints",
        json={"url": "https://example.com/hook", "events": ["event.finalized"]},
        headers=auth,
    )
    eid = create.json()["id"]
    r = await client.get(f"/v1/webhook-endpoints/{eid}/deliveries", headers=auth)
    assert r.status_code == 200
    assert r.json() == []
