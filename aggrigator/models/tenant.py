"""Tenant tables — mirror of MDProject users + per-user API key.

The aggrigator is single-tenant from a network-perimeter standpoint (only
MDProject ever calls it), but it tracks one ``TenantUser`` row per
MDProject user so analytics requests can be (a) attributed to a specific
user for audit/abuse purposes and (b) gated on that user's subscription
tier without re-asking MDProject on every call.

The source of truth for identity is MDProject (``core_user.User.public_id``);
this table is a mirror kept in sync via the signed ``/v1/internal/*`` API
(see ``subscription-plan/04-internal-api.md``).

The ``auth_user`` table remains for admin login only — these tables are
separate by design so the two surfaces can evolve independently.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aggrigator.models.base import Base, TimestampMixin


class TenantTier:
    FREE = "FREE"
    PRO = "PRO"


class TenantStatus:
    """Mirrors MDProject's Subscription.status — Stripe-derived."""
    ACTIVE = "active"
    TRIALING = "trialing"
    PAST_DUE = "past_due"
    UNPAID = "unpaid"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired"


class TenantUser(Base, TimestampMixin):
    __tablename__ = "tenant_user"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    # The MDProject User.public_id — the join key for every internal-API
    # call. Unique so we can upsert idempotently on provisioning.
    # unique=True only — migration 0009 created just the constraint (its
    # implicit index covers lookups); index=True here would make
    # autogenerate demand a second, redundant index forever.
    external_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False,
    )
    # Denormalized from MDProject for audit-log readability. CITEXT so
    # case-insensitive lookups match MDProject's email field semantics.
    email: Mapped[str] = mapped_column(CITEXT(), unique=True, nullable=False)
    tier: Mapped[str] = mapped_column(
        String(16),
        default=TenantTier.FREE,
        server_default=TenantTier.FREE,
        nullable=False,
    )
    # Mirrors MDProject's Subscription.status verbatim so the gate can use
    # the same grace-period logic on both sides (file 05 §6).
    status: Mapped[str] = mapped_column(
        String(24),
        default=TenantStatus.ACTIVE,
        server_default=TenantStatus.ACTIVE,
        nullable=False,
    )
    # Forward-compatible feature flags. Today: ``{"analytics": true|false}``.
    # Tomorrow: any per-tier capability without a schema migration.
    features: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False,
    )
    # Soft delete. Hard delete happens via a separate retention job after
    # 90 days for GDPR (see subscription-plan/06-user-flows.md §6).
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    api_keys = relationship(
        "TenantApiKey",
        back_populates="tenant_user",
        cascade="all, delete-orphan",
        lazy="raise",
    )


class TenantApiKey(Base, TimestampMixin):
    __tablename__ = "tenant_api_key"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tenant_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Plaintext lookup key, format ``agg_{env}_{head5}`` from
    # ``aggrigator/security/api_keys.py``. Indexed + unique so the
    # tenant-key auth dep can do an O(1) lookup before the argon2 hash
    # compare. TEXT (not VARCHAR(N)) so env-name changes don't force a
    # column widening.
    prefix: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # argon2id hash of the secret tail (everything after the prefix).
    # Plaintext lives only in MDProject's User.aggrigator_api_key column
    # — returned by /v1/internal/users exactly once at provisioning.
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # Last 4 chars of the raw key — display only, for ops "which key did
    # I just revoke?" confirmation. Never a secret.
    last_four: Mapped[str] = mapped_column(String(4), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    tenant_user = relationship(
        "TenantUser", back_populates="api_keys", lazy="raise",
    )
