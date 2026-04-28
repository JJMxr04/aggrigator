"""``/v1/webhook-endpoints`` — owner-scoped CRUD per plan §3.5.

Outbound webhook configuration. The signing secret is generated server-side,
encrypted at rest, and shown to the user **once** at create or rotate. The
delivery list is read-only (workers write rows; users observe).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from aggrigator.config import Settings, get_settings
from aggrigator.deps import SessionDep, require_user
from aggrigator.models import User, WebhookDelivery, WebhookEndpoint
from aggrigator.schemas.webhook import (
    WebhookDeliveryDetailOut,
    WebhookDeliveryOut,
    WebhookEndpointCreatedOut,
    WebhookEndpointIn,
    WebhookEndpointOut,
    WebhookEndpointPatchIn,
    WebhookEndpointRotatedOut,
)
from aggrigator.security.webhook_signing import (
    encrypt_secret,
    generate_secret,
)

router = APIRouter(prefix="/v1/webhook-endpoints", tags=["webhooks"])


SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("", response_model=list[WebhookEndpointOut])
async def list_endpoints(
    session: SessionDep,
    user: Annotated[User, Depends(require_user)],
) -> list[WebhookEndpoint]:
    rows = await session.scalars(
        select(WebhookEndpoint)
        .where(WebhookEndpoint.user_id == user.id)
        .order_by(WebhookEndpoint.created.desc())
    )
    return list(rows)


@router.post("", response_model=WebhookEndpointCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    payload: WebhookEndpointIn,
    session: SessionDep,
    settings: SettingsDep,
    user: Annotated[User, Depends(require_user)],
) -> WebhookEndpointCreatedOut:
    secret = generate_secret()
    row = WebhookEndpoint(
        user_id=user.id,
        url=str(payload.url),
        description=payload.description,
        events=list(payload.events),
        secret_ciphertext=encrypt_secret(secret, key=settings.secret_encryption_key),
        scope=payload.scope,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    base = WebhookEndpointOut.model_validate(row).model_dump()
    return WebhookEndpointCreatedOut(**base, signing_secret=secret)


@router.patch("/{endpoint_id}", response_model=WebhookEndpointOut)
async def patch_endpoint(
    endpoint_id: uuid.UUID,
    payload: WebhookEndpointPatchIn,
    session: SessionDep,
    user: Annotated[User, Depends(require_user)],
) -> WebhookEndpoint:
    row = await _get_owned(session, user, endpoint_id)
    if payload.url is not None:
        row.url = str(payload.url)
    if payload.description is not None:
        row.description = payload.description
    if payload.events is not None:
        row.events = list(payload.events)
    if payload.enabled is not None:
        row.enabled = payload.enabled
    await session.commit()
    await session.refresh(row)
    return row


@router.post("/{endpoint_id}/rotate", response_model=WebhookEndpointRotatedOut)
async def rotate_secret(
    endpoint_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    user: Annotated[User, Depends(require_user)],
) -> WebhookEndpointRotatedOut:
    row = await _get_owned(session, user, endpoint_id)
    secret = generate_secret()
    row.secret_ciphertext = encrypt_secret(secret, key=settings.secret_encryption_key)
    await session.commit()
    return WebhookEndpointRotatedOut(id=row.id, signing_secret=secret)


@router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(
    endpoint_id: uuid.UUID,
    session: SessionDep,
    user: Annotated[User, Depends(require_user)],
) -> None:
    row = await _get_owned(session, user, endpoint_id)
    await session.delete(row)
    await session.commit()


@router.get("/{endpoint_id}/deliveries", response_model=list[WebhookDeliveryOut])
async def list_deliveries(
    endpoint_id: uuid.UUID,
    session: SessionDep,
    user: Annotated[User, Depends(require_user)],
    delivered: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[WebhookDelivery]:
    await _get_owned(session, user, endpoint_id)
    stmt = (
        select(WebhookDelivery)
        .where(WebhookDelivery.endpoint_id == endpoint_id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(limit)
    )
    if delivered is True:
        stmt = stmt.where(WebhookDelivery.delivered_at.is_not(None))
    elif delivered is False:
        stmt = stmt.where(WebhookDelivery.delivered_at.is_(None))
    return list(await session.scalars(stmt))


@router.get(
    "/{endpoint_id}/deliveries/{delivery_id}",
    response_model=WebhookDeliveryDetailOut,
)
async def get_delivery(
    endpoint_id: uuid.UUID,
    delivery_id: uuid.UUID,
    session: SessionDep,
    user: Annotated[User, Depends(require_user)],
) -> WebhookDelivery:
    await _get_owned(session, user, endpoint_id)
    row = await session.get(WebhookDelivery, delivery_id)
    if row is None or row.endpoint_id != endpoint_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "delivery not found")
    return row


# ---- helper ---------------------------------------------------------------


async def _get_owned(
    session, user: User, endpoint_id: uuid.UUID,
) -> WebhookEndpoint:
    row = await session.get(WebhookEndpoint, endpoint_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "endpoint not found")
    return row
