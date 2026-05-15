"""WebhookDelivery — outbound delivery queue + retry/audit log.

Single hardcoded receiver (MDProject); the target URL and HMAC secret are
read from settings at dispatch time. The old per-tenant ``WebhookEndpoint``
table was dropped when aggrigator collapsed to single-tenant.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aggrigator.models.base import Base


class WebhookDelivery(Base):
    """One per event-state snapshot. See ``webhooks/idempotency.py`` for the
    key derivation. Payload + signature are frozen at enqueue time; retries
    re-sign with a fresh ``t`` but the body never changes.
    """

    __tablename__ = "webhook_delivery"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_webhook_delivery_idempotency_key",
        ),
        Index(
            "ix_webhook_delivery_due",
            "next_retry_at",
            postgresql_where="delivered_at IS NULL",
        ),
        Index("ix_webhook_delivery_event_id", "event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("core_event_event.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_name: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default="now()",
    )
