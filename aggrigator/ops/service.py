"""CronService — the read/write surface the API + HTML routes both depend on.

Keeps SQL out of the route handlers and gives tests one seam to mock if they
want to skip Redis.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.db import session_scope
from aggrigator.models import (
    CronRun,
    CronRunSource,
    CronRunStatus,
    CronSchedule,
    User,
)
from aggrigator.ops import lock as lock_module
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
    # True if the cron is wired into ARQ's schedule (workers/settings.py).
    # Manual-only entries don't render the enable/disable toggle.
    is_scheduled: bool = False
    # True if the scheduled tick is currently allowed to run. Default True
    # when no cron_schedule row exists yet (first deploy after migration).
    enabled: bool = True
    # Computed at render time when is_running=True so the card shows
    # how long the in-flight run has been going.
    elapsed_seconds: float | None = None
    run_id: int | None = None


class TriggerRejected(Exception):
    """A run of this cron is already in flight (lock collision)."""


class LockUnavailable(Exception):
    """The Redis lock subsystem is unreachable. The trigger never started.

    Distinct from TriggerRejected (which means "another run is already
    holding the lock"). LockUnavailable means we couldn't even ask Redis
    whether the lock was free — typically AGG_REDIS_URL is missing,
    pointing at the wrong host, or the Redis plugin is down.
    """


class CronService:
    def __init__(self, session: AsyncSession, redis: Redis):
        self.session = session
        self.redis = redis

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
            ValueError: cron is not on the ARQ schedule — refuse to toggle
                manual-only crons so we never surface a stale ``enabled``
                state in the UI for something the scheduler can't act on.
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
        """Acquire the lock, run the cron synchronously, return the resulting
        CronRun.

        Raises:
            KeyError: ``name`` is not a registered cron spec.
            TriggerRejected: another run is already holding the lock.
            LockUnavailable: Redis lock subsystem is unreachable. Best-effort
                ``cron_run`` row written so the failure shows up in history.
        """
        spec = by_name(name)
        if spec is None:
            raise KeyError(f"Unknown cron: {name}")

        run_id = str(uuid.uuid4())
        try:
            async with lock_module.cron_lock(
                self.redis, spec.name, run_id, ttl_seconds=spec.max_runtime_seconds,
            ) as acquired:
                if not acquired:
                    raise TriggerRejected(
                        f"a run of {spec.name} is already in flight"
                    )

                try:
                    row, _summary, _err = await run_with_recording(
                        spec,
                        trigger_source=CronRunSource.MANUAL,
                        started_by_user_id=actor.id,
                        arq_job_id=run_id,
                    )
                except BaseException:  # noqa: BLE001 — error already recorded
                    # Re-fetch so we return the updated row.
                    row = await self._latest_run(spec.name)
        except TriggerRejected:
            raise
        except Exception as exc:  # noqa: BLE001
            # Lock subsystem failure (Redis down, bad URL, auth, network).
            # Without this branch the request 500s and the operator sees no
            # cron_run row — making it look like the click did nothing.
            logger.error(
                "cron trigger aborted: cron=%s actor=%s redis_lock_error=%s: %s",
                spec.name, actor.id, type(exc).__name__, exc,
            )
            row = await self._record_lock_failure(
                spec, actor.id, run_id, exc,
            )
            raise LockUnavailable(
                f"cron lock unavailable for {spec.name}: {exc}"
            ) from exc

        email = await self._email_of(actor.id)
        return CronRunOut.from_row(row, email) if row else None

    async def _record_lock_failure(
        self,
        spec: CronSpec,
        actor_id: uuid.UUID | None,
        run_id: str,
        exc: BaseException,
    ) -> CronRun | None:
        """Best-effort: write a FAILED cron_run row for the lock failure.

        Uses its own session_scope so we don't poison the caller's session
        with an aborted transaction. If the DB is also down, log and return
        None — the caller still raises LockUnavailable for the client.
        """
        try:
            async with session_scope() as fail_session:
                row = CronRun(
                    cron_name=spec.name,
                    trigger_source=CronRunSource.MANUAL,
                    started_by_user_id=actor_id,
                    arq_job_id=run_id,
                    status=CronRunStatus.FAILED,
                    finished_at=datetime.now(tz=timezone.utc),
                    error=(
                        f"LockUnavailable: {type(exc).__name__}: {exc}\n\n"
                        "Redis lock subsystem unreachable. Check AGG_REDIS_URL "
                        "on the agg-web service and that the Redis plugin is up."
                    )[:4000],
                )
                fail_session.add(row)
                await fail_session.flush()
                return row
        except Exception as db_exc:  # noqa: BLE001
            logger.error(
                "could not record lock-failure cron_run row for %s: %s: %s",
                spec.name, type(db_exc).__name__, db_exc,
            )
            return None

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
