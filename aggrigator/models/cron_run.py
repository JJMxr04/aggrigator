"""``cron_run`` — append-only record of every cron execution (scheduled or manual).

Per plan §2.1.4. Both the ARQ wrapper (`cron_run_recorder`) and manual triggers
write rows here; the operator UI reads them.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aggrigator.models.base import Base


class CronRunStatus:
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class CronRunSource:
    SCHEDULED = "scheduled"
    MANUAL = "manual"


class CronRun(Base):
    __tablename__ = "cron_run"
    __table_args__ = (
        Index("ix_cron_run_cron_name_started_at", "cron_name", "started_at"),
        Index(
            "ix_cron_run_running",
            "cron_name",
            postgresql_where="status = 'running'",
        ),
        Index("ix_cron_run_arq_job_id", "arq_job_id", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cron_name: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_source: Mapped[str] = mapped_column(String(16), nullable=False)
    started_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth_user.id", ondelete="SET NULL"),
        nullable=True,
    )
    arq_job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=CronRunStatus.RUNNING
    )
    # Truncated to 4000 chars at write time per plan §2.1.8.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    items_processed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
