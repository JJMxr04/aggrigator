"""The /ops/logo-backfill admin page is gated and the run endpoint enforces CSRF."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_page_redirects_anonymous_to_login(client):
    # No admin session cookie → bounce to the SQLAdmin login (303).
    r = await client.get("/ops/logo-backfill", follow_redirects=False)
    assert r.status_code == 303
    assert "/admin/login" in r.headers["location"]


async def test_run_rejects_without_csrf(client):
    # No admin session AND no CSRF → must not execute a backfill.
    r = await client.post(
        "/ops/logo-backfill/run",
        data={"league_id": "usa-nba"},
        follow_redirects=False,
    )
    assert r.status_code in (401, 403)
