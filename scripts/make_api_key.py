"""Mint an API key for an existing user. Prints the raw key exactly once.

Usage:
    ./venv/bin/python scripts/make_api_key.py <email> [name]

If ``name`` is omitted, defaults to ``"mdproject-portal"``. The key is shown
in plaintext on stdout; capture it immediately — we only store the argon2
hash, so there is no recovery path.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from aggrigator.config import get_settings
from aggrigator.db import session_scope
from aggrigator.models.auth import ApiKey, User
from aggrigator.security.api_keys import generate_key


async def main(email: str, name: str) -> None:
    settings = get_settings()
    env_label = "live" if settings.env in ("prod", "production") else settings.env
    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            print(f"ERROR: no user with email {email!r}", file=sys.stderr)
            sys.exit(1)
        minted = generate_key(env=env_label)
        session.add(
            ApiKey(
                user_id=user.id,
                name=name,
                prefix=minted.prefix,
                key_hash=minted.key_hash,
                last_four=minted.last_four,
            )
        )
    print(f"Created API key for {email}")
    print(f"  name:   {name}")
    print(f"  prefix: {minted.prefix}")
    print(f"  key:    {minted.raw}")
    print()
    print("Store the key now — only the argon2 hash is persisted.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    email = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else "mdproject-portal"
    asyncio.run(main(email, name))
