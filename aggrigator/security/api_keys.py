"""Stripe-style API keys.

Format: ``agg_{env}_{head5}{tail}`` where ``head+tail = base32(rand24bytes).lower()``
(39 chars). The "prefix" ends after ``head5`` — a plaintext, indexed lookup key.

Storage:
- ``prefix`` is plaintext (``"agg_live_abcde"``), indexed, used to look up the
  key row in O(1).
- ``key_hash`` is argon2id over the secret tail (everything after the prefix).
  Never store raw.
- ``last_four`` is the last 4 chars of the secret, plaintext, for UI display.

The full key is shown to the user **once** at creation; after that there's no
way to recover it. Rotation = revoke + issue.

``env`` is variable-length (``"live"``, ``"dev"``, ``"test"``); split functions
parse by structure (split on ``_``), not a fixed prefix length.
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass

from passlib.hash import argon2

# Same argon2 params as passwords — argon2id, 64 MiB, t=3, p=4.
_HASHER = argon2.using(
    type="ID",
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    digest_size=32,
    salt_size=16,
)

HEAD_LEN = 5
LAST_FOUR_LEN = 4


@dataclass(frozen=True)
class GeneratedKey:
    """Returned to the caller exactly once, at key creation."""
    raw: str           # the value the user sees and stores
    prefix: str        # plaintext lookup key (also embedded in ``raw``)
    key_hash: str      # argon2 hash of ``tail`` — what we store
    last_four: str     # display-only


def generate_key(*, env: str = "live") -> GeneratedKey:
    """Mints a fresh key. ``env`` is short and embedded in the prefix so a
    leaked key reveals which deploy it came from (``agg_live_…`` vs
    ``agg_test_…``).
    """
    if not env or not env.isalnum():
        raise ValueError("env must be a non-empty alphanumeric string")

    rand = secrets.token_bytes(24)
    encoded = base64.b32encode(rand).decode("ascii").rstrip("=").lower()
    head = encoded[:HEAD_LEN]
    tail = encoded[HEAD_LEN:]

    prefix = f"agg_{env}_{head}"
    raw = prefix + tail
    key_hash = _HASHER.hash(tail)
    last_four = raw[-LAST_FOUR_LEN:]

    return GeneratedKey(raw=raw, prefix=prefix, key_hash=key_hash, last_four=last_four)


def split_raw(raw: str) -> tuple[str, str] | None:
    """``"agg_live_abcdefxxxx"`` -> ``("agg_live_abcde", "fxxxx")``.

    Parses by structure: ``agg_{env}_{head5}{tail}``. Returns ``None`` for
    anything that doesn't match.
    """
    if not raw or not raw.startswith("agg_"):
        return None
    parts = raw.split("_", 2)
    if len(parts) != 3:
        return None
    _, env, after = parts
    if not env.isalnum() or len(after) <= HEAD_LEN:
        return None
    head = after[:HEAD_LEN]
    tail = after[HEAD_LEN:]
    return f"agg_{env}_{head}", tail


def verify_key(raw: str, *, expected_prefix: str, key_hash: str) -> bool:
    """Returns True iff ``raw`` matches ``expected_prefix`` and the tail
    hashes to ``key_hash``. Constant-time prefix compare + argon2 verify.
    """
    parts = split_raw(raw)
    if parts is None:
        return False
    prefix, tail = parts
    if not secrets.compare_digest(prefix, expected_prefix):
        return False
    try:
        return _HASHER.verify(tail, key_hash)
    except (ValueError, TypeError):
        return False
