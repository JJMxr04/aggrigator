"""TeamLogo — odds-api crest bytes cached in the aggregator's own Postgres.

1:1 with ``core_team`` (PK = the team's composite id). Kept in a side table
so hot Team queries never drag the BYTEA column around. ``status`` is
``ok`` (bytes present) or ``missing`` (odds-api returned 404 — negative
cached until the cooldown elapses; see ingest/logos.py).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from aggrigator.models.base import Base, TimestampMixin


class TeamLogo(Base, TimestampMixin):
    __tablename__ = "core_team_logo"

    team_id: Mapped[str] = mapped_column(
        String(80),
        ForeignKey("core_team.id", ondelete="CASCADE"),
        primary_key=True,
    )
    image: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    byte_size: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    etag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # "ok" | "missing"
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(
        String(32), default="odds-api", server_default="odds-api", nullable=False
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
