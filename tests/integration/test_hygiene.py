"""P0-4 (/metrics token) + P1-5 (body cap)."""

from __future__ import annotations

import pytest

from aggrigator.config import get_settings

pytestmark = pytest.mark.asyncio


# ---- /metrics ---------------------------------------------------------------


async def test_metrics_open_when_no_token_configured(client) -> None:
    assert (await client.get("/metrics")).status_code == 200


async def test_metrics_404_without_token(client, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "metrics_token", "sekret-token")
    r = await client.get("/metrics")
    assert r.status_code == 404  # hide-existence, not 401


async def test_metrics_404_with_wrong_token(client, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "metrics_token", "sekret-token")
    r = await client.get(
        "/metrics", headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 404


async def test_metrics_200_with_bearer_token(client, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "metrics_token", "sekret-token")
    r = await client.get(
        "/metrics", headers={"Authorization": "Bearer sekret-token"},
    )
    assert r.status_code == 200


# ---- body-size cap ----------------------------------------------------------


async def test_oversized_body_is_413(client) -> None:
    blob = "x" * (1024 * 1024 + 1)
    r = await client.post(
        "/v1/auth/login", json={"email": "a@b.com", "password": blob},
    )
    assert r.status_code == 413


async def test_normal_body_unaffected(client) -> None:
    r = await client.post(
        "/v1/auth/login", json={"email": "a@b.com", "password": "wrong"},
    )
    assert r.status_code == 401  # passed the cap, failed auth as expected
