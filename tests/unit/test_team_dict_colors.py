"""Unit: webhook _team_dict carries all four colors (design §6)."""

from __future__ import annotations

from types import SimpleNamespace

from aggrigator.webhooks.payload import _team_dict


def _team():
    return SimpleNamespace(
        team_id="USA", league_id="VNL",
        name_long="United States", name_medium="USA", name_short="USA",
        primary_color="#0A3161", secondary_color="#B31942",
        primary_contrast="#FFFFFF", secondary_contrast="#000000",
        stat_entity_id="home",
    )


def test_team_dict_includes_all_four_colors():
    d = _team_dict(_team())
    assert d["primary_color"] == "#0A3161"
    assert d["secondary_color"] == "#B31942"
    assert d["primary_contrast"] == "#FFFFFF"
    assert d["secondary_contrast"] == "#000000"


def test_team_dict_none_passthrough():
    assert _team_dict(None) is None
