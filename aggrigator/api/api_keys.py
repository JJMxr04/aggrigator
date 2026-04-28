"""``/v1/keys`` — owner-scoped API key management."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from aggrigator.config import get_settings
from aggrigator.deps import SessionDep, require_user
from aggrigator.models.auth import ApiKey, User
from aggrigator.schemas.auth import ApiKeyCreatedOut, ApiKeyCreateIn, ApiKeyOut
from aggrigator.security.api_keys import generate_key

router = APIRouter(prefix="/v1/keys", tags=["api-keys"])


def _env_label() -> str:
    """``"live"`` for ``env=prod``, otherwise mirror the env name."""
    settings = get_settings()
    return "live" if settings.env in ("prod", "production") else settings.env


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(
    session: SessionDep, user: Annotated[User, Depends(require_user)]
) -> list[ApiKey]:
    rows = await session.scalars(
        select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
    )
    return list(rows)


@router.post("", response_model=ApiKeyCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_key(
    payload: ApiKeyCreateIn,
    session: SessionDep,
    user: Annotated[User, Depends(require_user)],
) -> ApiKeyCreatedOut:
    minted = generate_key(env=_env_label())
    row = ApiKey(
        user_id=user.id,
        name=payload.name,
        prefix=minted.prefix,
        key_hash=minted.key_hash,
        last_four=minted.last_four,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return ApiKeyCreatedOut.model_validate(
        {**ApiKeyOut.model_validate(row).model_dump(), "key": minted.raw}
    )


@router.post("/{key_id}/rotate", response_model=ApiKeyCreatedOut)
async def rotate_key(
    key_id: uuid.UUID,
    session: SessionDep,
    user: Annotated[User, Depends(require_user)],
) -> ApiKeyCreatedOut:
    """Revokes the existing key and issues a new one with the same name."""
    old = await _get_owned(session, user, key_id)
    if old.revoked_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "key already revoked")
    old.revoked_at = datetime.now(tz=timezone.utc)

    minted = generate_key(env=_env_label())
    new_row = ApiKey(
        user_id=user.id,
        name=old.name,
        prefix=minted.prefix,
        key_hash=minted.key_hash,
        last_four=minted.last_four,
    )
    session.add(new_row)
    await session.commit()
    await session.refresh(new_row)
    return ApiKeyCreatedOut.model_validate(
        {**ApiKeyOut.model_validate(new_row).model_dump(), "key": minted.raw}
    )


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(
    key_id: uuid.UUID,
    session: SessionDep,
    user: Annotated[User, Depends(require_user)],
) -> None:
    row = await _get_owned(session, user, key_id)
    if row.revoked_at is None:
        row.revoked_at = datetime.now(tz=timezone.utc)
        await session.commit()


async def _get_owned(session, user: User, key_id: uuid.UUID) -> ApiKey:
    row = await session.get(ApiKey, key_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "key not found")
    return row
