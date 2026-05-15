"""Unit tests for security.webhook_signing — sign, verify, parse_header."""

from __future__ import annotations

import time

import pytest

from aggrigator.security.webhook_signing import (
    DEFAULT_MAX_SKEW_SECONDS,
    InvalidSignature,
    parse_header,
    sign,
    verify,
)


SECRET = "supersecret-webhook-token"
BODY = b'{"hello":"world"}'


# ---- sign / verify --------------------------------------------------------


def test_sign_then_verify_round_trip() -> None:
    header = sign(secret=SECRET, body=BODY)
    verify(secret=SECRET, body=BODY, header_value=str(header))  # no raise


def test_verify_rejects_tampered_body() -> None:
    header = sign(secret=SECRET, body=BODY)
    with pytest.raises(InvalidSignature):
        verify(secret=SECRET, body=BODY + b"!", header_value=str(header))


def test_verify_rejects_tampered_signature() -> None:
    header = sign(secret=SECRET, body=BODY)
    bad = str(header)[:-2] + "00"
    with pytest.raises(InvalidSignature):
        verify(secret=SECRET, body=BODY, header_value=bad)


def test_verify_rejects_wrong_secret() -> None:
    header = sign(secret=SECRET, body=BODY)
    with pytest.raises(InvalidSignature):
        verify(secret="other-secret", body=BODY, header_value=str(header))


def test_verify_rejects_stale_timestamp() -> None:
    past = int(time.time()) - DEFAULT_MAX_SKEW_SECONDS - 60
    header = sign(secret=SECRET, body=BODY, timestamp=past)
    with pytest.raises(InvalidSignature, match="out of window"):
        verify(secret=SECRET, body=BODY, header_value=str(header))


def test_verify_rejects_far_future_timestamp() -> None:
    future = int(time.time()) + DEFAULT_MAX_SKEW_SECONDS + 60
    header = sign(secret=SECRET, body=BODY, timestamp=future)
    with pytest.raises(InvalidSignature, match="out of window"):
        verify(secret=SECRET, body=BODY, header_value=str(header))


def test_verify_accepts_within_skew() -> None:
    edge = int(time.time()) - (DEFAULT_MAX_SKEW_SECONDS - 5)
    header = sign(secret=SECRET, body=BODY, timestamp=edge)
    verify(secret=SECRET, body=BODY, header_value=str(header))  # no raise


def test_two_signatures_at_different_t_differ() -> None:
    a = sign(secret=SECRET, body=BODY, timestamp=1000)
    b = sign(secret=SECRET, body=BODY, timestamp=2000)
    assert a.signature != b.signature


# ---- header parsing -------------------------------------------------------


def test_parse_header_known_shape() -> None:
    ts, sig = parse_header("t=12345,v1=deadbeef")
    assert ts == 12345
    assert sig == "deadbeef"


@pytest.mark.parametrize("bad", [
    "",
    "no-equals-sign",
    "t=12345",                 # missing v1
    "v1=deadbeef",             # missing t
    "t=not-a-number,v1=abc",
])
def test_parse_header_rejects_malformed(bad: str) -> None:
    with pytest.raises(InvalidSignature):
        parse_header(bad)
