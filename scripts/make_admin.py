"""Promote a user to ``role='admin'`` (or create one).

Usage:
    ./venv/bin/python scripts/make_admin.py admin@example.com [password]

If the user exists, just sets ``role='admin'`` (and reactivates if disabled).
If not, creates them with the given password (a fresh random one is printed
if you omit it). Idempotent.
"""

from __future__ import annotations

import asyncio
import secrets
import sys

from sqlalchemy import select

from aggrigator.db import session_scope
from aggrigator.models.auth import User, UserRole
from aggrigator.security.passwords import hash_password

# Operator-set passwords (CLI second arg) must clear this floor. The
# generated default (``secrets.token_urlsafe(16)`` ≈ 22 chars) always
# does. Floor exists because a typo of "a" or "test" on the bootstrap
# command would otherwise create a trivially brute-forceable admin.
_MIN_PASSWORD_LEN = 16


async def main(email: str, password: str | None) -> None:
    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            pw = password or secrets.token_urlsafe(16)
            user = User(
                email=email,
                password_hash=hash_password(pw),
                role=UserRole.ADMIN,
                is_active=True,
            )
            session.add(user)
            print(f"Created admin {email}")
            if password is None:
                print(f"  generated password: {pw}")
        else:
            user.role = UserRole.ADMIN
            user.is_active = True
            if password is not None:
                user.password_hash = hash_password(password)
                print(f"Promoted {email} to admin and updated password")
            else:
                print(f"Promoted {email} to admin (password unchanged)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    email = sys.argv[1]
    password = sys.argv[2] if len(sys.argv) > 2 else None
    if password is not None and len(password) < _MIN_PASSWORD_LEN:
        print(
            f"error: password must be at least {_MIN_PASSWORD_LEN} characters "
            f"(got {len(password)}). Omit the password arg to auto-generate "
            "a secure one.",
            file=sys.stderr,
        )
        sys.exit(2)
    asyncio.run(main(email, password))
