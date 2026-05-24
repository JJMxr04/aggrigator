"""One-shot deploy-state probe for the webhook pipeline.

Run inside the deployed container (Railway / Coolify) when delivery from
aggregator → MDProject looks broken. Prints a single text report covering
the four things that can independently go wrong:

1. **Config** — is ``AGG_MDPROJECT_URL`` / ``AGG_WEBHOOK_SECRET`` set?
   (Values masked; we just confirm presence.)
2. **Alembic** — is the DB at head? Was migration 0015 (procrastinate
   schema) applied?
3. **Procrastinate** — do the queue tables exist and are there jobs queued?
4. **webhook_delivery** — how many rows are unsent, retried, permanently
   failed; how many recent ``webhook_deliver`` runs are in cron_run.

Usage:
    ./venv/bin/python scripts/probe_webhook_state.py

Exit code is always 0 — this is diagnostic, not a healthcheck. Read the
output and act on the SUMMARY lines at the bottom.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

from aggrigator.config import get_settings
from aggrigator.db import session_scope


def _yn(value: object) -> str:
    return "YES" if value else "NO"


def _mask(value: str) -> str:
    if not value:
        return "<unset>"
    if len(value) <= 8:
        return "•" * len(value)
    return value[:4] + "•" * (len(value) - 8) + value[-4:]


async def _check_config() -> list[str]:
    s = get_settings()
    lines = ["[1] Config"]
    lines.append(f"  AGG_MDPROJECT_URL set     : {_yn(s.mdproject_url)}  ({s.mdproject_url or '<unset>'})")
    lines.append(f"  AGG_WEBHOOK_SECRET set    : {_yn(s.webhook_secret)}  ({_mask(s.webhook_secret)})")
    lines.append(f"  derived webhook_target_url: {s.webhook_target_url or '<unset>'}")
    return lines


async def _check_alembic(session) -> list[str]:
    lines = ["[2] Alembic"]
    try:
        row = (await session.execute(text("SELECT version_num FROM alembic_version"))).first()
        current = row[0] if row else "<empty>"
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  ERROR querying alembic_version: {type(exc).__name__}: {exc}")
        return lines
    lines.append(f"  current head: {current}")
    # The procrastinate migration introduces the queue schema. If this
    # one hasn't been applied, ``notify_webhook_worker`` will fail.
    expected_procrastinate = "0015_procrastinate_schema"
    lines.append(
        f"  procrastinate schema migration ({expected_procrastinate}) applied: "
        f"{_yn(current >= '0015')}"
    )
    return lines


async def _check_procrastinate(session) -> list[str]:
    lines = ["[3] Procrastinate"]
    try:
        present = (await session.execute(text(
            "SELECT to_regclass('public.procrastinate_jobs') IS NOT NULL"
        ))).scalar()
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  ERROR introspecting procrastinate_jobs: {type(exc).__name__}: {exc}")
        return lines
    lines.append(f"  procrastinate_jobs table exists: {_yn(present)}")
    if not present:
        lines.append("  → defer_async() will fail; webhook pushes are dropped.")
        return lines
    try:
        rows = (await session.execute(text(
            "SELECT status, COUNT(*) FROM procrastinate_jobs GROUP BY status ORDER BY status"
        ))).all()
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  ERROR counting procrastinate_jobs: {type(exc).__name__}: {exc}")
        return lines
    if not rows:
        lines.append("  procrastinate_jobs: (empty)")
    else:
        for status, n in rows:
            lines.append(f"  procrastinate_jobs[{status}] = {n}")
    return lines


async def _check_webhook_delivery(session) -> list[str]:
    from aggrigator.models import WebhookDelivery
    lines = ["[4] webhook_delivery"]
    total = (await session.execute(select(func.count(WebhookDelivery.id)))).scalar() or 0
    delivered = (await session.execute(
        select(func.count(WebhookDelivery.id))
        .where(WebhookDelivery.delivered_at.isnot(None))
    )).scalar() or 0
    undelivered = total - delivered
    perma_failed = (await session.execute(
        select(func.count(WebhookDelivery.id))
        .where(
            WebhookDelivery.delivered_at.is_(None),
            WebhookDelivery.next_retry_at.is_(None),
            WebhookDelivery.attempts > 0,
        )
    )).scalar() or 0
    retry_pending = (await session.execute(
        select(func.count(WebhookDelivery.id))
        .where(
            WebhookDelivery.delivered_at.is_(None),
            WebhookDelivery.next_retry_at.isnot(None),
        )
    )).scalar() or 0
    never_attempted = (await session.execute(
        select(func.count(WebhookDelivery.id))
        .where(
            WebhookDelivery.delivered_at.is_(None),
            WebhookDelivery.attempts == 0,
        )
    )).scalar() or 0
    lines.append(f"  total      : {total}")
    lines.append(f"  delivered  : {delivered}")
    lines.append(f"  undelivered: {undelivered}")
    lines.append(f"    never attempted : {never_attempted}  (worker never picked them up)")
    lines.append(f"    retry pending   : {retry_pending}  (backoff in progress)")
    lines.append(f"    permanently fail: {perma_failed}  (4xx or attempts exhausted)")
    return lines


async def _check_cron_runs(session) -> list[str]:
    from aggrigator.models import CronRun
    lines = ["[5] webhook_deliver cron_run history (last 24h)"]
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    rows = (await session.execute(
        select(CronRun.status, func.count(CronRun.id))
        .where(
            CronRun.cron_name == "webhook_deliver",
            CronRun.started_at >= cutoff,
        )
        .group_by(CronRun.status)
    )).all()
    if not rows:
        lines.append("  no webhook_deliver runs in last 24h — worker may not be running.")
        return lines
    for status, n in rows:
        lines.append(f"  {status}: {n}")
    last = (await session.execute(
        select(CronRun.started_at, CronRun.finished_at, CronRun.status)
        .where(CronRun.cron_name == "webhook_deliver")
        .order_by(CronRun.started_at.desc())
        .limit(1)
    )).first()
    if last:
        lines.append(f"  most recent: started={last[0]} finished={last[1]} status={last[2]}")
    return lines


async def main() -> None:
    print("=" * 64)
    print("aggrigator webhook pipeline probe — ", datetime.now(tz=timezone.utc).isoformat())
    print("=" * 64)
    for line in await _check_config():
        print(line)
    print()
    async with session_scope() as session:
        for line in await _check_alembic(session):
            print(line)
        print()
        for line in await _check_procrastinate(session):
            print(line)
        print()
        for line in await _check_webhook_delivery(session):
            print(line)
        print()
        for line in await _check_cron_runs(session):
            print(line)
    print()
    print("=" * 64)
    print("Common diagnoses:")
    print("  - 'undelivered > 0' + 'no recent cron_run rows' = worker not running.")
    print("    Try Run from /ops/crons (webhook_deliver) to drain manually.")
    print("  - 'AGG_MDPROJECT_URL <unset>' = enqueue silently drops; fix env + restart.")
    print("  - 'procrastinate_jobs table exists: NO' = run alembic upgrade head.")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
