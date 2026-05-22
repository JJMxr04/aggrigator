"""``cron_schedule`` — per-cron enabled flag for the ARQ scheduler.

One row per registered cron. ``enabled=False`` makes the scheduled tick a
no-op (the ``cron_run_recorder`` short-circuits before opening a row).
Manual triggers through ``/ops/crons/{name}/run`` still work — disable
pauses the scheduler, not the manual path.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from aggrigator.models.base import Base


class CronSchedule(Base):
    __tablename__ = "cron_schedule"

    cron_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth_user.id", ondelete="SET NULL"),
        nullable=True,
    )
