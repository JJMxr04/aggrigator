"""Paradise channel — ``/v1/internal/*``.

HMAC-signed (``X-Paradise-Signature``), MDProject-only. Three operations:

1. ``POST /v1/internal/users`` — provision a tenant user + issue an API
   key. Idempotent: 201 + plaintext key on insert; 200 (no key) on
   already-exists.
2. ``PATCH /v1/internal/users/{external_user_id}/entitlements`` — sync
   tier + status from MDProject's Stripe webhook handler.
3. ``POST /v1/internal/users/{external_user_id}/api-keys/rotate`` —
   revoke the existing key, mint a new one, return plaintext once.

The plaintext key is the **only** secret ever returned by this surface
and only at provisioning / rotation. Everything else is verified via the
prefix + argon2 hash already stored.

See ``subscription-plan/04-internal-api.md`` for the full contract.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select

from aggrigator.config import get_settings
from aggrigator.deps import SessionDep, require_paradise_signed
from aggrigator.models.tenant import (
    TenantApiKey,
    TenantStatus,
    TenantTier,
    TenantUser,
)
from aggrigator.security.api_keys import generate_key

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/internal",
    tags=["internal"],
    # Every route in this router is signature-gated. Applying at the
    # router level means a future contributor adding an endpoint can't
    # forget the dep.
    dependencies=[Depends(require_paradise_signed)],
)


# ---- request / response models -----------------------------------------


_VALID_TIERS = {TenantTier.FREE, TenantTier.PRO}
_VALID_STATUSES = {
    TenantStatus.ACTIVE,
    TenantStatus.TRIALING,
    TenantStatus.PAST_DUE,
    TenantStatus.UNPAID,
    TenantStatus.CANCELED,
    TenantStatus.INCOMPLETE,
    TenantStatus.INCOMPLETE_EXPIRED,
}


class ProvisionUserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_user_id: uuid.UUID
    email: EmailStr
    tier: str = Field(default=TenantTier.FREE)
    status: str = Field(default=TenantStatus.ACTIVE)
    features: dict = Field(default_factory=dict)


class EntitlementUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: str
    status: str
    features: dict = Field(default_factory=dict)


# ---- helpers -----------------------------------------------------------


def _check_tier_status(tier: str, status_: str) -> None:
    if tier not in _VALID_TIERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid tier: {tier!r} (must be one of {sorted(_VALID_TIERS)})",
        )
    if status_ not in _VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid status: {status_!r}",
        )


async def _issue_key(session, tenant_user: TenantUser) -> str:
    """Mint a fresh API key for ``tenant_user``, persist its hash, return
    the plaintext exactly once. Caller owns the commit."""
    gen = generate_key(env=get_settings().env)
    session.add(
        TenantApiKey(
            tenant_user_id=tenant_user.id,
            prefix=gen.prefix,
            key_hash=gen.key_hash,
            last_four=gen.last_four,
        )
    )
    return gen.raw


# ---- routes ------------------------------------------------------------


@router.post("/users")
async def provision_user(payload: ProvisionUserIn, session: SessionDep):
    """Create a tenant_user + initial api_key for an MDProject user.

    Idempotent: re-calling with the same ``external_user_id`` returns the
    existing row (200, no key in body). MDProject calls this from the
    user-signup signal and from the one-shot backfill script.
    """
    _check_tier_status(payload.tier, payload.status)

    existing = await session.scalar(
        select(TenantUser).where(
            TenantUser.external_user_id == payload.external_user_id
        )
    )
    if existing is not None:
        return {
            "tenant_user_id": str(existing.id),
            "external_user_id": str(existing.external_user_id),
            "tier": existing.tier,
            "status": existing.status,
            "features": existing.features,
            "created": False,
        }

    tenant_user = TenantUser(
        external_user_id=payload.external_user_id,
        email=str(payload.email),
        tier=payload.tier,
        status=payload.status,
        features=payload.features,
    )
    session.add(tenant_user)
    await session.flush()  # populate tenant_user.id for the FK below

    plaintext = await _issue_key(session, tenant_user)
    await session.commit()
    await session.refresh(tenant_user)

    return _json_201({
        "tenant_user_id": str(tenant_user.id),
        "external_user_id": str(tenant_user.external_user_id),
        "tier": tenant_user.tier,
        "status": tenant_user.status,
        "features": tenant_user.features,
        "api_key": plaintext,
        "api_key_returned_once": True,
        "created": True,
    })


@router.patch("/users/{external_user_id}/entitlements")
async def update_entitlements(
    external_user_id: uuid.UUID,
    payload: EntitlementUpdateIn,
    session: SessionDep,
):
    """Push tier + status changes from MDProject's Stripe webhook handler.

    Idempotent: re-sending the same body is a no-op apart from the
    ``updated`` timestamp. Callers should treat 404 as "we don't know
    that user — re-run provision_user first" (a defensive path)."""
    _check_tier_status(payload.tier, payload.status)

    tenant_user = await session.scalar(
        select(TenantUser).where(TenantUser.external_user_id == external_user_id)
    )
    if tenant_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="unknown tenant user — provision first",
        )

    tenant_user.tier = payload.tier
    tenant_user.status = payload.status
    tenant_user.features = payload.features
    await session.commit()
    await session.refresh(tenant_user)

    return {
        "external_user_id": str(tenant_user.external_user_id),
        "tier": tenant_user.tier,
        "status": tenant_user.status,
        "features": tenant_user.features,
        "applied_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/users/{external_user_id}/api-keys/rotate")
async def rotate_api_key(external_user_id: uuid.UUID, session: SessionDep):
    """Revoke every live key for this user, issue one new one, return
    plaintext exactly once. Used for DB-restore recovery + suspected leak.

    NB: revokes ALL active keys (defensive — we don't expect more than
    one live key per user, but if there are stragglers, this is the
    safer move than rotating only the latest).
    """
    tenant_user = await session.scalar(
        select(TenantUser).where(TenantUser.external_user_id == external_user_id)
    )
    if tenant_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="unknown tenant user",
        )

    now = datetime.now(timezone.utc)
    live_keys = list(await session.scalars(
        select(TenantApiKey).where(
            TenantApiKey.tenant_user_id == tenant_user.id,
            TenantApiKey.revoked_at.is_(None),
        )
    ))
    for k in live_keys:
        k.revoked_at = now

    plaintext = await _issue_key(session, tenant_user)
    await session.commit()

    return _json_201({
        "external_user_id": str(tenant_user.external_user_id),
        "api_key": plaintext,
        "api_key_returned_once": True,
        "revoked_count": len(live_keys),
    })


# ---- 201 helper --------------------------------------------------------
# FastAPI uses the default 200 unless you raise or return a Response. The
# provisioning + rotation paths both want 201 to signal "new resource
# minted". The cleanest path is JSONResponse with an explicit status.
from fastapi.responses import JSONResponse  # noqa: E402 — bottom by convention


def _json_201(body: dict) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=body)
