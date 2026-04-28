"""Argon2id password hashing.

Parameters per plan §8: ``time_cost=3``, ``memory_cost=64MiB``, ``parallelism=4``,
``hash_len=32``, ``salt_len=16``. Tuning these is a deploy-time decision; tests
set the same numbers so they run in similar time on dev hardware.
"""

from __future__ import annotations

from passlib.hash import argon2

# Argon2id with the §8 parameters. ``type="ID"`` selects argon2id specifically
# (the default in passlib≥1.7.4 for newer hashes, but we pin it explicitly).
_HASHER = argon2.using(
    type="ID",
    time_cost=3,
    memory_cost=65536,   # 64 MiB
    parallelism=4,
    digest_size=32,
    salt_size=16,
)


def hash_password(plaintext: str) -> str:
    """Returns the encoded argon2id hash (with embedded params + salt)."""
    if not plaintext:
        raise ValueError("password must be non-empty")
    return _HASHER.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    """Constant-time verify. Returns False on bad inputs rather than raising."""
    if not plaintext or not hashed:
        return False
    try:
        return _HASHER.verify(plaintext, hashed)
    except (ValueError, TypeError):
        return False


def needs_rehash(hashed: str) -> bool:
    """True if the hash was made with weaker params than ``_HASHER`` produces.

    Call after a successful verify; if True, re-hash and persist so users
    transparently upgrade to current params on their next login.
    """
    try:
        return _HASHER.needs_update(hashed)
    except ValueError:
        return True
