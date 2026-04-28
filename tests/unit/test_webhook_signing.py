"""Unit tests for security.webhook_signing — sign, verify, encrypt, decrypt."""

from __future__ import annotations

import time

import pytest

from aggrigator.security.webhook_signing import (
    DEFAULT_MAX_SKEW_SECONDS,
    InvalidSignature,
    decrypt_secret,
    encrypt_secret,
    fernet_key_from_passphrase,
    generate_secret,
    parse_header,
    sign,
    verify,
)


SECRET = "supersecret-webhook-token"
KEY = fernet_key_from_passphrase("test-passphrase")
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


# ---- secret generation + encryption --------------------------------------


def test_generate_secret_is_unique_and_url_safe() -> None:
    a = generate_secret()
    b = generate_secret()
    assert a != b
    assert all(c.isalnum() or c in "-_" for c in a)


def test_encrypt_decrypt_round_trip() -> None:
    enc = encrypt_secret("hunter2", key=KEY)
    assert enc != "hunter2"
    assert decrypt_secret(enc, key=KEY) == "hunter2"


def test_decrypt_wrong_key_raises() -> None:
    enc = encrypt_secret("hunter2", key=KEY)
    other = fernet_key_from_passphrase("different")
    with pytest.raises(InvalidSignature):
        decrypt_secret(enc, key=other)
