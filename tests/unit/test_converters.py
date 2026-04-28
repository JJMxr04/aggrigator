"""Unit tests for converters.american_to_decimal & friends."""

from decimal import Decimal

import pytest

from aggrigator.ingest.converters import (
    american_to_decimal,
    decimal_to_american,
    decimal_to_fractional,
    movement_label,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+130", Decimal("2.3000")),
        ("130", Decimal("2.3000")),
        ("-150", Decimal("1.6667")),
        ("+100", Decimal("2.0000")),
        ("-100", Decimal("2.0000")),
    ],
)
def test_american_to_decimal_known(raw: str, expected: Decimal) -> None:
    assert american_to_decimal(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "abc", "0"])
def test_american_to_decimal_none_for_garbage(raw) -> None:
    assert american_to_decimal(raw) is None


def test_decimal_to_american_round_trip_positive() -> None:
    assert decimal_to_american(Decimal("2.3000")) == 130


def test_decimal_to_american_round_trip_negative() -> None:
    assert decimal_to_american(Decimal("1.6667")) == -150


def test_decimal_to_american_at_one_returns_zero() -> None:
    assert decimal_to_american(Decimal("1.0")) == 0


def test_decimal_to_fractional_basic() -> None:
    assert decimal_to_fractional(Decimal("2.0")) == "1/1"
    assert decimal_to_fractional(Decimal("1.5")) == "1/2"


def test_decimal_to_fractional_floor_at_one() -> None:
    assert decimal_to_fractional(Decimal("1.0")) == "0/1"


@pytest.mark.parametrize(
    "change,label",
    [(0, "flat"), (1, "up"), (-1, "down"), (5, "up"), (-99, "down")],
)
def test_movement_label(change: int, label: str) -> None:
    assert movement_label(change) == label
