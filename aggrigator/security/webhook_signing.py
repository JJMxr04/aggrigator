"""Webhook signing — Stripe-style HMAC-SHA256.

Header format: ``X-Aggrigator-Signature: t=<unix_ts>,v1=<hex>`` where the
HMAC is over ``f"{t}.{raw_body}"`` using the shared signing secret. Receivers
MUST verify the timestamp is within ``max_skew`` (default 5 minutes) before
trusting the message — that's the replay defense.

Single hardcoded receiver (MDProject) shares one secret with us via env vars
(``AGG_WEBHOOK_SECRET`` here, ``AGGRIGATOR_WEBHOOK_SECRET`` there). No
at-rest encryption layer remains.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

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
    of ``now`` (default 5 minutes).
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
