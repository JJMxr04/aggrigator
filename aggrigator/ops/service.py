"""CronService — the read/write surface the API + HTML routes both depend on.

Keeps SQL out of the route handlers and gives tests one seam to mock if they
want to skip Redis.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.models import CronRun, CronRunSource, CronRunStatus, User
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


class TriggerRejected(Exception):
    """A run of this cron is already in flight (lock collision)."""


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
            items.append(CronListItem(
                name=spec.name,
                description=spec.description,
                schedule_human=spec.schedule_human,
                last_run=CronRunOut.from_row(row, email) if row else None,
                is_running=row is not None and row.status == CronRunStatus.RUNNING,
            ))
        return items

    async def get_cron(self, name: str) -> CronListItem | None:
        spec = by_name(name)
        if spec is None:
            return None
        row = await self._latest_run(spec.name)
        email = await self._email_of(row.started_by_user_id) if row else None
        return CronListItem(
            name=spec.name,
            description=spec.description,
            schedule_human=spec.schedule_human,
            last_run=CronRunOut.from_row(row, email) if row else None,
            is_running=row is not None and row.status == CronRunStatus.RUNNING,
        )

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
        CronRun. Raises ``TriggerRejected`` if a run of this cron is already
        in flight."""
        spec = by_name(name)
        if spec is None:
            raise KeyError(f"Unknown cron: {name}")

        run_id = str(uuid.uuid4())
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

        email = await self._email_of(actor.id)
        return CronRunOut.from_row(row, email)

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
