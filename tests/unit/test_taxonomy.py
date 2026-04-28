"""Unit tests for ingest.taxonomy — oddID parsing + market_type generation."""

import pytest

from aggrigator.ingest.taxonomy import (
    BET_TYPE_TO_CATEGORY,
    PERIOD_TO_SCOPE,
    SIDE_TO_SELECTION,
    OddIDParts,
    market_type_for,
    parse_odd_id,
)


def test_parse_odd_id_5_parts() -> None:
    parts = parse_odd_id("points-home-game-ml-home")
    assert parts == OddIDParts("points", "home", "game", "ml", "home")


def test_parse_odd_id_player_prop_keeps_underscores_in_entity() -> None:
    # Player props use underscored playerIDs in the statEntityID slot.
    parts = parse_odd_id("points-TRAVIS_KELCE_1_NFL-game-ou-over")
    assert parts.statEntityID == "TRAVIS_KELCE_1_NFL"
    assert parts.betTypeID == "ou"


@pytest.mark.parametrize("bad", ["points-home-game-ml", "a-b-c-d-e-f", "no-dashes"])
def test_parse_odd_id_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_odd_id(bad)


def test_market_type_default_pattern() -> None:
    parts = OddIDParts("points", "home", "game", "ml", "home")
    assert market_type_for(parts, "NFL") == "NFL_POINTS_ML"


def test_market_type_btts_override() -> None:
    parts = OddIDParts("goals", "all", "game", "yn", "yes")
    assert market_type_for(parts, "MLS") == "MLS_BTTS"


def test_market_type_nhl_puck_line_override() -> None:
    parts = OddIDParts("goals", "home", "game", "sp", "home")
    assert market_type_for(parts, "NHL") == "NHL_PUCK_LINE"


def test_market_type_mlb_run_line_override() -> None:
    parts = OddIDParts("runs", "home", "game", "sp", "home")
    assert market_type_for(parts, "MLB") == "MLB_RUN_LINE"


def test_market_type_override_doesnt_apply_to_wrong_league() -> None:
    """The puck line override is gated on league=NHL — for NBA it's the default."""
    parts = OddIDParts("goals", "home", "game", "sp", "home")
    assert market_type_for(parts, "NBA") == "NBA_GOALS_SP"


def test_taxonomy_tables_are_consistent() -> None:
    # Every category we map a betType into must be one of the known categories.
    known_categories = {"MONEYLINE", "SPREAD", "TOTAL", "PROPS_GAME", "PROPS_TEAM"}
    assert set(BET_TYPE_TO_CATEGORY.values()) <= known_categories
    # Every period we map must produce a scope value that's a non-empty string.
    assert all(isinstance(v, str) and v for v in PERIOD_TO_SCOPE.values())
    # Every side we map must be a known SelectionType label.
    known_sides = {"HOME", "AWAY", "DRAW", "OVER", "UNDER", "YES", "NO", "EVEN", "ODD"}
    assert set(SIDE_TO_SELECTION.values()) <= known_sides
