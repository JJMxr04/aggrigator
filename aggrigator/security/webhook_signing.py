"""Webhook signing — Stripe-style HMAC-SHA256.

Header format (per plan §4.2): ``X-Aggrigator-Signature: t=<unix_ts>,v1=<hex>``
where the HMAC is over ``f"{t}.{raw_body}"`` using the endpoint's signing
secret. Receivers MUST verify the timestamp is within ``max_skew`` (default 5
minutes) before trusting the message — that's the replay defense.

The signing secret is stored encrypted at rest (Fernet); ``encrypt_secret`` /
``decrypt_secret`` here are also used by the endpoint-CRUD API.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as _secrets
import time
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

HEADER_NAME = "X-Aggrigator-Signature"
SIGNATURE_VERSION = "v1"
DEFAULT_MAX_SKEW_SECONDS = 300


class InvalidSignature(Exception):
    """Header malformed / stale / mismatched / wrong secret."""


@dataclass(frozen=True)
class SignedHeader:
    """``t=<ts>,v1=<hex>`` — the value of ``X-Aggrigator-Signature``."""
    timestamp: int
    signature: str

    def __str__(self) -> str:
        return f"t={self.timestamp},{SIGNATURE_VERSION}={self.signature}"


def generate_secret(num_bytes: int = 32) -> str:
    """Mints a fresh URL-safe signing secret. Show to user once."""
    return _secrets.token_urlsafe(num_bytes)


# ---- at-rest encryption ---------------------------------------------------


def _fernet(key: str) -> Fernet:
    """Build a Fernet cipher. ``key`` must be a 44-char base64 string."""
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_secret(plaintext: str, *, key: str) -> str:
    return _fernet(key).encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str, *, key: str) -> str:
    try:
        return _fernet(key).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise InvalidSignature("encryption key changed; re-rotate webhook secret") from exc


# ---- signing ---------------------------------------------------------------


def sign(*, secret: str, body: bytes, timestamp: int | None = None) -> SignedHeader:
    """Returns the ``X-Aggrigator-Signature`` header value to attach to a POST."""
    ts = timestamp or int(time.time())
    msg = f"{ts}.".encode() + body
    digest = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return SignedHeader(timestamp=ts, signature=digest)


def parse_header(header_value: str) -> tuple[int, str]:
    """Returns ``(timestamp, signature)`` or raises ``InvalidSignature``."""
    if not header_value:
        raise InvalidSignature("missing header")
    parts = {}
    for kv in header_value.split(","):
        kv = kv.strip()
        if "=" not in kv:
            raise InvalidSignature(f"malformed header part: {kv!r}")
        k, _, v = kv.partition("=")
        parts[k.strip()] = v.strip()
    if "t" not in parts or SIGNATURE_VERSION not in parts:
        raise InvalidSignature("header missing t or v1")
    try:
        ts = int(parts["t"])
    except ValueError as exc:
        raise InvalidSignature("non-integer t") from exc
    return ts, parts[SIGNATURE_VERSION]


def verify(
    *,
    secret: str,
    body: bytes,
    header_value: str,
    max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS,
    now: int | None = None,
) -> None:
    """Raises ``InvalidSignature`` if anything is off; returns ``None`` on OK.

    Replay protection is mandatory: ``t`` must be within ``max_skew_seconds``
    of ``now`` (default 5 minutes per plan §4.2).
    """
    ts, sig = parse_header(header_value)
    current = now or int(time.time())
    if abs(current - ts) > max_skew_seconds:
        raise InvalidSignature(
            f"timestamp out of window ({abs(current - ts)}s skew > {max_skew_seconds}s)"
        )
    expected = hmac.new(
        secret.encode(),
        f"{ts}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise InvalidSignature("signature mismatch")


# ---- helper for tests / smoke checks ---------------------------------------


def fernet_key_from_passphrase(passphrase: str) -> str:
    """Derive a Fernet key from a free-form passphrase. **Tests only** —
    production should use a real, randomly generated key."""
    digest = hashlib.sha256(passphrase.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()
