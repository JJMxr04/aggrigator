"""CronService — the read/write surface the API + HTML routes both depend on.

Keeps SQL out of the route handlers and gives tests one seam to mock if they
want to skip the queue.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.db import session_scope
from aggrigator.models import (
    CronRun,
    CronRunSource,
    CronRunStatus,
    CronSchedule,
    User,
)
from aggrigator.ops.recorder import run_with_recording
from aggrigator.ops.registry import CronSpec, REGISTRY, by_name

logger = logging.getLogger(__name__)


@dataclass
class CronRunOut:
    id: int
    cron_name: str
    trigger_source: str
    started_by_email: str | None
    started_at: datetime
    finished_at: datetime | None
    status: str
    duration_seconds: float | None
    items_processed: int | None
    error_excerpt: str | None

    @classmethod
    def from_row(cls, row: CronRun, user_email: str | None) -> "CronRunOut":
        duration = (
            (row.finished_at - row.started_at).total_seconds()
            if row.finished_at is not None
            else None
        )
        excerpt = (row.error or "")[:200] if row.error else None
        return cls(
            id=row.id,
            cron_name=row.cron_name,
            trigger_source=row.trigger_source,
            started_by_email=user_email,
            started_at=row.started_at,
            finished_at=row.finished_at,
            status=row.status,
            duration_seconds=duration,
            items_processed=row.items_processed,
            error_excerpt=excerpt,
        )


@dataclass
class CronListItem:
    name: str
    description: str
    schedule_human: str
    last_run: CronRunOut | None
    is_running: bool
    # True if the cron is wired into Procrastinate's periodic schedule
    # (``@app.periodic`` decorator on the task). Manual-only entries
    # don't render the enable/disable toggle.
    is_scheduled: bool = False
    # True if the scheduled tick is currently allowed to run. Default True
    # when no cron_schedule row exists yet (first deploy after migration).
    enabled: bool = True
    # Computed at render time when is_running=True so the card shows
    # how long the in-flight run has been going.
    elapsed_seconds: float | None = None
    run_id: int | None = None


class TriggerRejected(Exception):
    """A run of this cron is already in flight (the per-cron advisory
    lock is held). Surfaced as HTTP 409 — the operator clicks again
    once the in-flight run finishes."""


def _advisory_key(cron_name: str) -> int:
    """Stable signed 64-bit int from a cron name, for pg_advisory_lock.

    Postgres advisory-lock keys are bigint (int64); we hash the cron name
    to fit. The MSB is masked off so the value stays non-negative — Postgres
    accepts the full int64 range but a positive key avoids two's-complement
    surprises in logs.
    """
    digest = hashlib.blake2b(cron_name.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=False) & 0x7FFF_FFFF_FFFF_FFFF


class CronService:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ---- list / detail ------------------------------------------------------

    async def list_crons(self) -> list[CronListItem]:
        items: list[CronListItem] = []
        for spec in REGISTRY:
            row = await self._latest_run(spec.name)
            email = await self._email_of(row.started_by_user_id) if row else None
            items.append(await self._build_list_item(spec, row, email))
        return items

    async def get_cron(self, name: str) -> CronListItem | None:
        spec = by_name(name)
        if spec is None:
            return None
        row = await self._latest_run(spec.name)
        email = await self._email_of(row.started_by_user_id) if row else None
        return await self._build_list_item(spec, row, email)

    async def _build_list_item(
        self, spec: CronSpec, row: CronRun | None, email: str | None,
    ) -> CronListItem:
        is_running = row is not None and row.status == CronRunStatus.RUNNING
        elapsed_seconds: float | None = None
        run_id: int | None = None
        if is_running and row is not None:
            elapsed_seconds = (
                datetime.now(tz=row.started_at.tzinfo) - row.started_at
            ).total_seconds()
            run_id = row.id
        enabled = await self._enabled(spec.name)
        return CronListItem(
            name=spec.name,
            description=spec.description,
            schedule_human=spec.schedule_human,
            last_run=CronRunOut.from_row(row, email) if row else None,
            is_running=is_running,
            is_scheduled=spec.is_scheduled,
            enabled=enabled,
            elapsed_seconds=elapsed_seconds,
            run_id=run_id,
        )

    # ---- enable / disable --------------------------------------------------

    async def _enabled(self, name: str) -> bool:
        """Default True when no row exists — first deploy after the migration
        starts every cron in the active state."""
        row = await self.session.get(CronSchedule, name)
        return True if row is None else row.enabled

    async def set_enabled(
        self, name: str, *, enabled: bool, actor: User,
    ) -> CronListItem:
        """Upsert the cron_schedule row and return the refreshed list item.

        Raises:
            KeyError: ``name`` is not in the registry.
            ValueError: cron is not on the periodic schedule — refuse to
                toggle manual-only crons so we never surface a stale
                ``enabled`` state in the UI for something the scheduler
                can't act on.
        """
        spec = by_name(name)
        if spec is None:
            raise KeyError(f"Unknown cron: {name}")
        if not spec.is_scheduled:
            raise ValueError(
                f"{name} is manual-only — no scheduled tick to toggle"
            )
        async with session_scope() as write_session:
            row = await write_session.get(CronSchedule, name)
            if row is None:
                row = CronSchedule(
                    cron_name=name,
                    enabled=enabled,
                    updated_by_user_id=actor.id,
                )
                write_session.add(row)
            else:
                row.enabled = enabled
                row.updated_by_user_id = actor.id
        # Refresh the caller's view through the existing path so the
        # returned item reflects the new state.
        item = await self.get_cron(name)
        assert item is not None
        return item

    async def history(self, name: str, *, limit: int = 25) -> list[CronRunOut]:
        rows = list(await self.session.scalars(
            select(CronRun)
            .where(CronRun.cron_name == name)
            .order_by(CronRun.started_at.desc())
            .limit(limit)
        ))
        out: list[CronRunOut] = []
        for r in rows:
            email = await self._email_of(r.started_by_user_id)
            out.append(CronRunOut.from_row(r, email))
        return out

    async def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = await self.session.get(CronRun, run_id)
        if row is None:
            return None
        email = await self._email_of(row.started_by_user_id)
        return {
            **CronRunOut.from_row(row, email).__dict__,
            "summary": row.summary,
            "error_full": row.error,
        }

    # ---- trigger ------------------------------------------------------------

    async def trigger(
        self,
        name: str,
        *,
        actor: User,
    ) -> CronRunOut:
        """Acquire the advisory lock, run the cron inline, return the row.

        The lock keys off the cron name via ``pg_try_advisory_lock`` and is
        held on a dedicated session for the duration of the run — releases
        automatically when that session closes (covers crash paths). Scheduled
        runs in the worker take the same lock at the top of
        ``cron_run_recorder``, so a scheduled tick that fires while a manual
        run is in flight skips itself rather than racing.

        Raises:
            KeyError: ``name`` is not a registered cron spec.
            TriggerRejected: another run holds the advisory lock.
        """
        spec = by_name(name)
        if spec is None:
            raise KeyError(f"Unknown cron: {name}")

        lock_key = _advisory_key(spec.name)
        run_id = str(uuid.uuid4())

        # The lock-holding session is separate from any session
        # ``run_with_recording`` opens internally. Holding it on its own
        # connection means a clean release on ``__aexit__`` even if the
        # cron body raises mid-flight; and if the gunicorn worker dies
        # uncleanly, the connection death also releases the lock.
        async with session_scope() as lock_session:
            got = await lock_session.scalar(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": lock_key},
            )
            if not got:
                raise TriggerRejected(
                    f"a run of {spec.name} is already in flight"
                )
            try:
                try:
                    row, _summary, _err = await run_with_recording(
                        spec,
                        trigger_source=CronRunSource.MANUAL,
                        started_by_user_id=actor.id,
                        job_id=run_id,
                    )
                except BaseException:  # noqa: BLE001 — error already recorded
                    # Re-fetch so we return the updated row.
                    row = await self._latest_run(spec.name)
            finally:
                await lock_session.execute(
                    text("SELECT pg_advisory_unlock(:k)"),
                    {"k": lock_key},
                )

        email = await self._email_of(actor.id)
        return CronRunOut.from_row(row, email) if row else None

    # ---- internals ----------------------------------------------------------

    async def _latest_run(self, cron_name: str) -> CronRun | None:
        return await self.session.scalar(
            select(CronRun)
            .where(CronRun.cron_name == cron_name)
            .order_by(CronRun.started_at.desc())
            .limit(1)
        )

    async def _email_of(self, user_id: uuid.UUID | None) -> str | None:
        if user_id is None:
            return None
        u = await self.session.get(User, user_id)
        return u.email if u else None
