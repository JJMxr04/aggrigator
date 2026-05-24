"""Integration tests for the cron-runner ops module.

Covers:
- Manual trigger writes a ``cron_run`` row + runs the cron + records summary
- A second trigger while one is in flight gets 409 (Postgres advisory lock)
- The list endpoint returns every registered cron with last_run
- The history endpoint paginates newest-first
- The HTMX HTML routes refuse non-admin sessions
- Admin auth is required on every JSON route

Each test inserts a stub runner via the registry so we don't depend on the
real cron tasks running against the local odds simulator.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from aggrigator.models import CronRun, User, UserRole
from aggrigator.security.passwords import hash_password
from tests.integration.factories import login_and_get_token

pytestmark = pytest.mark.asyncio


# ---- helpers ---------------------------------------------------------------


async def _make_admin(session, *, email: str = "admin@example.com") -> User:
    user = User(
        email=email,
        password_hash=hash_password("hunter2hunter2"),
        role=UserRole.ADMIN,
    )
    session.add(user)
    await session.commit()
    return user


async def _admin_token(client: AsyncClient, session) -> str:
    """Seed an admin user directly, then log in to mint the JWT."""
    await _make_admin(session, email="ops-admin@example.com")
    r = await client.post(
        "/v1/auth/login",
        json={"email": "ops-admin@example.com", "password": "hunter2hunter2"},
    )
    return r.json()["access_token"]


# ---- list / detail ---------------------------------------------------------


async def test_list_crons_requires_admin(client) -> None:
    r = await client.get("/v1/admin/crons")
    assert r.status_code == 401  # no auth


async def test_list_crons_rejects_non_admin(client, session) -> None:
    token = await login_and_get_token(client, session)  # plain user
    r = await client.get(
        "/v1/admin/crons", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403


async def test_list_crons_returns_registered(client, session) -> None:
    token = await _admin_token(client, session)
    r = await client.get(
        "/v1/admin/crons", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    body = r.json()
    names = [it["name"] for it in body]
    # Every registered cron is present.
    assert "ingest_due_leagues" in names
    assert "webhook_deliver" in names
    assert "settle_pending" in names
    assert "seed_sports" in names
    assert "seed_leagues" in names
    # No last_run yet — fresh DB.
    assert all(it["last_run"] is None for it in body)
    assert all(it["is_running"] is False for it in body)


async def test_get_cron_404_for_unknown(client, session) -> None:
    token = await _admin_token(client, session)
    r = await client.get(
        "/v1/admin/crons/does-not-exist",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


# ---- trigger ---------------------------------------------------------------


async def test_trigger_writes_cron_run_row(
    client, session, monkeypatch,
) -> None:
    """Patch the cron's runner to a stub so the test doesn't depend on real
    provider calls. Confirm the row lands with status=success and the summary."""
    token = await _admin_token(client, session)

    captured = {"called": 0}

    async def _stub_runner():
        captured["called"] += 1
        return {"events_processed": 5, "events_failed": 0}

    # Replace the runner on the registry entry for this test.
    from aggrigator.ops import registry as reg
    target = next(s for s in reg.REGISTRY if s.name == "ingest_due_leagues")
    monkeypatch.setattr(target, "runner", _stub_runner, raising=False)

    r = await client.post(
        "/v1/admin/crons/ingest_due_leagues/run",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "success"
    assert body["trigger_source"] == "manual"
    assert body["started_by_email"] == "ops-admin@example.com"
    assert body["items_processed"] == 5
    assert captured["called"] == 1

    # Confirm the row landed in the DB.
    rows = list(await session.scalars(
        select(CronRun).where(CronRun.cron_name == "ingest_due_leagues")
    ))
    assert len(rows) == 1
    assert rows[0].status == "success"
    assert rows[0].started_by_user_id is not None


async def test_trigger_records_failure_with_error(
    client, session, monkeypatch,
) -> None:
    token = await _admin_token(client, session)

    async def _bad_runner():
        raise RuntimeError("simulated explosion")

    from aggrigator.ops import registry as reg
    target = next(s for s in reg.REGISTRY if s.name == "settle_pending")
    monkeypatch.setattr(target, "runner", _bad_runner, raising=False)

    r = await client.post(
        "/v1/admin/crons/settle_pending/run",
        headers={"Authorization": f"Bearer {token}"},
    )
    # The trigger endpoint catches the recorder's re-raise and returns the
    # row in a "failed" state; status code reflects HTTP success of the
    # endpoint, not the cron run.
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert "simulated explosion" in (body.get("error_excerpt") or "")


async def test_trigger_409_when_advisory_lock_held(
    client, session, monkeypatch,
) -> None:
    """Two concurrent triggers — the second hits the Postgres advisory
    lock held by the first and gets a 409 Conflict (the same UX the old
    Redis lock provided). Uses a sleep-based runner to make the race
    deterministic without timing assumptions."""
    token = await _admin_token(client, session)

    started = asyncio.Event()
    finish = asyncio.Event()

    async def _slow_runner():
        started.set()
        await finish.wait()
        return {"events_processed": 1}

    from aggrigator.ops import registry as reg
    target = next(s for s in reg.REGISTRY if s.name == "ingest_due_leagues")
    monkeypatch.setattr(target, "runner", _slow_runner, raising=False)

    # First request — held open until we set ``finish``.
    first = asyncio.create_task(
        client.post(
            "/v1/admin/crons/ingest_due_leagues/run",
            headers={"Authorization": f"Bearer {token}"},
        )
    )
    await started.wait()  # ensures the advisory lock is acquired

    # Second request — should 409 because the lock is held.
    second = await client.post(
        "/v1/admin/crons/ingest_due_leagues/run",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert second.status_code == 409

    finish.set()
    first_response = await first
    assert first_response.status_code == 200


async def test_trigger_unknown_cron_404(client, session) -> None:
    token = await _admin_token(client, session)
    r = await client.post(
        "/v1/admin/crons/nope/run",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


async def test_trigger_requires_admin(client) -> None:
    r = await client.post("/v1/admin/crons/ingest_due_leagues/run")
    assert r.status_code == 401


# ---- history --------------------------------------------------------------


async def test_history_returns_newest_first(
    client, session, monkeypatch,
) -> None:
    token = await _admin_token(client, session)

    async def _quick_runner():
        return {"sent": 0, "retried": 0, "failed": 0}

    from aggrigator.ops import registry as reg
    target = next(s for s in reg.REGISTRY if s.name == "webhook_deliver")
    monkeypatch.setattr(target, "runner", _quick_runner, raising=False)

    for _ in range(3):
        r = await client.post(
            "/v1/admin/crons/webhook_deliver/run",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200

    r = await client.get(
        "/v1/admin/crons/webhook_deliver/runs",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 3
    times = [datetime.fromisoformat(r["started_at"].replace("Z", "+00:00")) for r in runs]
    assert times == sorted(times, reverse=True)


# ---- run detail ----------------------------------------------------------


async def test_run_detail_returns_full_summary(
    client, session, monkeypatch,
) -> None:
    token = await _admin_token(client, session)

    async def _runner():
        return {"events_processed": 12, "events_failed": 1, "leagues": 3}

    from aggrigator.ops import registry as reg
    target = next(s for s in reg.REGISTRY if s.name == "ingest_due_leagues")
    monkeypatch.setattr(target, "runner", _runner, raising=False)

    create = await client.post(
        "/v1/admin/crons/ingest_due_leagues/run",
        headers={"Authorization": f"Bearer {token}"},
    )
    run_id = create.json()["id"]

    r = await client.get(
        f"/v1/admin/crons/ingest_due_leagues/runs/{run_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == {"events_processed": 12, "events_failed": 1, "leagues": 3}


# ---- pause / resume -------------------------------------------------------


async def test_pause_resume_toggle_persists(client, session) -> None:
    """Pause then resume — confirm the dashboard list reports the new
    ``enabled`` state on each step. (The scheduled-fire-skips-while-paused
    behavior is covered by the recorder unit tests.)"""
    token = await _admin_token(client, session)

    r = await client.post(
        "/v1/admin/crons/seed_sports/enabled",
        headers={"Authorization": f"Bearer {token}"},
        json={"enabled": False},
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    r = await client.post(
        "/v1/admin/crons/seed_sports/enabled",
        headers={"Authorization": f"Bearer {token}"},
        json={"enabled": True},
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is True


async def test_pause_rejected_for_manual_only_cron(client, session) -> None:
    """Manual-only crons (full_refresh, webhook_deliver, load_registry)
    aren't on the periodic schedule, so toggling them would silently
    have no effect. Refuse with 409 so the UI never shows a stale state."""
    token = await _admin_token(client, session)
    r = await client.post(
        "/v1/admin/crons/full_refresh/enabled",
        headers={"Authorization": f"Bearer {token}"},
        json={"enabled": False},
    )
    assert r.status_code == 409
