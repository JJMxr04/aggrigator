"""Unit tests for security.api_keys — generate / split / verify."""

from __future__ import annotations

import pytest

from aggrigator.security.api_keys import (
    HEAD_LEN,
    LAST_FOUR_LEN,
    generate_key,
    split_raw,
    verify_key,
)


def test_generate_key_shape_for_live() -> None:
    minted = generate_key(env="live")
    assert minted.raw.startswith("agg_live_")
    assert minted.prefix.startswith("agg_live_")
    # Prefix length: ``agg_`` + ``live`` + ``_`` + 5 chars = 14
    assert len(minted.prefix) == 14
    # Raw length: prefix + tail (39 - HEAD_LEN = 34)
    assert len(minted.raw) == 14 + (39 - HEAD_LEN)
    assert minted.last_four == minted.raw[-LAST_FOUR_LEN:]
    assert minted.key_hash.startswith("$argon2id$")


def test_generate_key_dev_env_has_shorter_prefix() -> None:
    minted = generate_key(env="dev")
    assert minted.prefix.startswith("agg_dev_")
    assert len(minted.prefix) == len("agg_dev_") + HEAD_LEN


def test_generate_key_rejects_bad_env() -> None:
    with pytest.raises(ValueError):
        generate_key(env="")
    with pytest.raises(ValueError):
        generate_key(env="not-alnum")


def test_generated_keys_are_unique() -> None:
    seen = {generate_key().raw for _ in range(20)}
    assert len(seen) == 20


def test_verify_round_trip() -> None:
    minted = generate_key(env="live")
    assert verify_key(minted.raw, expected_prefix=minted.prefix, key_hash=minted.key_hash)


def test_verify_rejects_wrong_prefix() -> None:
    minted = generate_key(env="live")
    assert not verify_key(
        minted.raw, expected_prefix="agg_test_xxxxx", key_hash=minted.key_hash
    )


def test_verify_rejects_tampered_tail() -> None:
    minted = generate_key(env="live")
    # Flip a char in the tail.
    bad = minted.raw[:-1] + ("a" if minted.raw[-1] != "a" else "b")
    assert not verify_key(bad, expected_prefix=minted.prefix, key_hash=minted.key_hash)


def test_verify_rejects_completely_garbage() -> None:
    minted = generate_key(env="live")
    assert not verify_key("", expected_prefix=minted.prefix, key_hash=minted.key_hash)
    assert not verify_key("not-a-key", expected_prefix=minted.prefix, key_hash=minted.key_hash)
    assert not verify_key("agg_live_short", expected_prefix=minted.prefix, key_hash=minted.key_hash)


def test_split_raw_known_shape() -> None:
    # Hand-construct a key-shaped string that doesn't depend on randomness.
    raw = "agg_live_abcdeXXXXXXXXXXXXXX"
    parts = split_raw(raw)
    assert parts is not None
    prefix, tail = parts
    assert prefix == "agg_live_abcde"
    assert tail == "XXXXXXXXXXXXXX"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "agg_live",          # no underscore separator after env
        "agg_live_",         # empty body
        "agg_live_short",    # body shorter than HEAD_LEN+1
        "wrong_live_xxxxxxxx",
        None,
    ],
)
def test_split_raw_rejects_malformed(bad) -> None:
    assert split_raw(bad) is None
