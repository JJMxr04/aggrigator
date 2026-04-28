"""Auth domain — User, ApiKey, RefreshToken, ClientApp.

Mirrors plan §2.2. CITEXT is used for ``User.email`` so logins are case-insensitive
without app-side normalization. ``ClientApp`` identifies the *application*
making the request (Django portal, Flutter iOS, …) and is the source of truth
for the §8 first-party rate-limit promotion.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import CITEXT, INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aggrigator.models.base import Base, TimestampMixin


class UserRole:
    ADMIN = "admin"
    USER = "user"


class UserTier:
    SERVICE = "service"      # first-party (MDProject, Flutter) — high ceiling, free
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class User(Base, TimestampMixin):
    __tablename__ = "auth_user"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(CITEXT(), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), default=UserRole.USER, nullable=False)
    tier: Mapped[str] = mapped_column(String(16), default=UserTier.FREE, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )


class ApiKey(Base):
    """Stripe-style API key — prefix is plain text (lookupable), key_hash is argon2id."""

    __tablename__ = "auth_api_key"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    prefix: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    last_four: Mapped[str] = mapped_column(String(4), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default="now()",
    )


class RefreshToken(Base):
    __tablename__ = "auth_refresh_token"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )


class ClientApp(Base, TimestampMixin):
    """Future-proofs the first-party promotion (plan §2.2).

    v1 seeds one row (slug='mdproject-django', tier='service'). When Flutter
    ships, two more rows are inserted; the rate-limit middleware promotes any
    request whose ``X-Client-App`` header matches a non-revoked, trusted,
    service-tier row.
    """

    __tablename__ = "client_app"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    client_secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[str] = mapped_column(String(16), default=UserTier.FREE, nullable=False)
    trusted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
