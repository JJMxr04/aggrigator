"""Unit tests for security.passwords — argon2id."""

from __future__ import annotations

import pytest

from aggrigator.security.passwords import (
    hash_password,
    needs_rehash,
    verify_password,
)


def test_hash_then_verify_round_trip() -> None:
    h = hash_password("correct horse battery staple")
    assert h.startswith("$argon2id$")
    assert verify_password("correct horse battery staple", h)


def test_verify_rejects_wrong_password() -> None:
    h = hash_password("hunter2")
    assert not verify_password("hunter3", h)


def test_each_hash_is_unique_due_to_salt() -> None:
    a = hash_password("same")
    b = hash_password("same")
    assert a != b
    assert verify_password("same", a)
    assert verify_password("same", b)


def test_empty_password_rejected() -> None:
    with pytest.raises(ValueError):
        hash_password("")


def test_verify_handles_garbage_inputs() -> None:
    assert not verify_password("", "$argon2id$irrelevant")
    assert not verify_password("anything", "")
    assert not verify_password("anything", "not-a-hash")


def test_needs_rehash_false_for_current_params() -> None:
    h = hash_password("ok")
    assert not needs_rehash(h)


def test_needs_rehash_true_for_garbage() -> None:
    assert needs_rehash("not-a-real-hash")
