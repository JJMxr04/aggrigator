"""Webhook subscription + delivery rows.

Mirrors plan §2.2 with one deliberate divergence: the plan said
``secret_hash`` (argon2), but the *sender* needs the raw secret to compute
HMAC-SHA256 signatures (plan §4.2). We store ``secret_ciphertext`` —
Fernet-encrypted with ``AGG_SECRET_ENCRYPTION_KEY`` — instead. The receiver
side (MDProject) holds its own copy of the raw secret and verifies.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aggrigator.models.base import Base, TimestampMixin


class WebhookEndpoint(Base, TimestampMixin):
    __tablename__ = "webhook_endpoint"
    __table_args__ = (
        Index("ix_webhook_endpoint_user_id_enabled", "user_id", "enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    events: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # 'full' | 'selected' (only 'full' implemented in v1; column exists so
    # adding 'selected' later is config-only — plan §4.5).
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="full", server_default="full"
    )


class WebhookDelivery(Base):
    """One per (endpoint × event-state-snapshot) — see plan §4.6 for the
    idempotency key. Payload + signature are frozen at enqueue time; retries
    re-sign with a fresh ``t`` but the body never changes.
    """

    __tablename__ = "webhook_delivery"
    __table_args__ = (
        UniqueConstraint(
            "endpoint_id", "idempotency_key",
            name="uq_webhook_delivery_endpoint_id_idempotency_key",
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
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("webhook_endpoint.id", ondelete="CASCADE"),
        nullable=False,
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
