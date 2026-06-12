"""Provision THE MDProject service tenant + key (roadmap Phase 2, plan §6.4).

Usage:
    ./venv/bin/python scripts/provision_service_tenant.py            # create (or show)
    ./venv/bin/python scripts/provision_service_tenant.py --rotate   # revoke old keys, mint new

Creates one TenantUser (email ``service@mdproject.internal``) and one API
key, then prints the THREE values the operator pastes into Coolify:

  - AGG_SERVICE_TENANT_EXTERNAL_ID  → aggrigator web service env
  - AGGRIGATOR_SERVICE_KEY          → BOTH MDProject services' env
  - (the key is shown exactly once — it's argon2-hashed at rest)

Idempotent: re-running without ``--rotate`` never invalidates anything;
it reports the existing tenant and refuses to print a key it can't know.
Run inside the web container (Coolify terminal) or anywhere with
AGG_DATABASE_URL pointing at the prod DB.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from aggrigator.db import session_scope
from aggrigator.models.tenant import TenantApiKey, TenantStatus, TenantTier, TenantUser
from aggrigator.security.api_keys import generate_key

SERVICE_EMAIL = "service@mdproject.internal"


def _print_block(external_id: uuid.UUID, raw_key: str | None) -> None:
    print()
    print("Coolify env values:")
    print(f"  AGG_SERVICE_TENANT_EXTERNAL_ID={external_id}   (aggrigator web)")
    if raw_key:
        print(f"  AGGRIGATOR_SERVICE_KEY={raw_key}   (MDProject web + worker)")
        print()
        print("The key above is shown ONCE — it is stored argon2-hashed.")
    else:
        print("  AGGRIGATOR_SERVICE_KEY=<unchanged — rerun with --rotate to mint a new one>")


async def main(rotate: bool) -> None:
    async with session_scope() as session:
        tenant = await session.scalar(
            select(TenantUser).where(TenantUser.email == SERVICE_EMAIL)
        )

        if tenant is None:
            generated = generate_key(env="live")
            tenant = TenantUser(
                external_user_id=uuid.uuid4(),
                email=SERVICE_EMAIL,
                # Tier is meaningless for the service tenant (plan §6.4 —
                # tier gating lives in MDProject); FREE documents that
                # nothing here relies on it.
                tier=TenantTier.FREE,
                status=TenantStatus.ACTIVE,
            )
            session.add(tenant)
            await session.flush()
            session.add(TenantApiKey(
                tenant_user_id=tenant.id,
                prefix=generated.prefix,
                key_hash=generated.key_hash,
                last_four=generated.last_four,
            ))
            print(f"Created service tenant {SERVICE_EMAIL}")
            _print_block(tenant.external_user_id, generated.raw)
            return

        if not rotate:
            print(f"Service tenant already exists ({SERVICE_EMAIL}).")
            _print_block(tenant.external_user_id, None)
            return

        # --rotate: revoke every live key, mint exactly one fresh one.
        keys = list(await session.scalars(
            select(TenantApiKey).where(
                TenantApiKey.tenant_user_id == tenant.id,
                TenantApiKey.revoked_at.is_(None),
            )
        ))
        now = datetime.now(tz=timezone.utc)
        for k in keys:
            k.revoked_at = now
        generated = generate_key(env="live")
        session.add(TenantApiKey(
            tenant_user_id=tenant.id,
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            last_four=generated.last_four,
        ))
        print(f"Rotated service key ({len(keys)} old key(s) revoked).")
        print("Update AGGRIGATOR_SERVICE_KEY on MDProject and redeploy BEFORE")
        print("the old key's traffic matters — there is no overlap window.")
        _print_block(tenant.external_user_id, generated.raw)


if __name__ == "__main__":
    asyncio.run(main(rotate="--rotate" in sys.argv[1:]))
