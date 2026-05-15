"""Audit log helpers.

Use ``await audit.write(session, event_name=..., ...)`` from any handler. The
helper never raises — audit writes failing must not break the request path.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.models.audit import AuditLog

logger = logging.getLogger(__name__)


# Canonical event names.
class Event:
    AUTH_LOGIN_OK = "auth.login.ok"
    AUTH_LOGIN_FAIL = "auth.login.fail"
    AUTH_REFRESH_OK = "auth.refresh.ok"
    AUTH_REFRESH_FAIL = "auth.refresh.fail"
    AUTH_LOGOUT = "auth.logout"
    WEBHOOK_DELIVERY_SUCCESS = "webhook.delivery.success"
    WEBHOOK_DELIVERY_FAIL = "webhook.delivery.fail"
    ADMIN_LOGIN = "admin.login"
    ADMIN_WRITE = "admin.write"


async def write(
    session: AsyncSession,
    *,
    event_name: str,
    actor_user_id: uuid.UUID | None = None,
    ip: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        session.add(AuditLog(
            actor_user_id=actor_user_id,
            ip=ip,
            event_name=event_name,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            audit_metadata=metadata,
        ))
        await session.flush()
    except Exception:  # noqa: BLE001 — never break the request path
        logger.exception("audit write failed for event_name=%s", event_name)
