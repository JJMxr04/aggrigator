"""GAP-C regression: analytics _team_dto must emit logo_url via
the keyless endpoint, not the (never-populated) Team.logo_url column."""

from __future__ import annotations

from types import SimpleNamespace

from aggrigator.api.analytics import _team_dto


def _fake_team(team_id: str = "NFL:DAL") -> SimpleNamespace:
    league_id, raw_id = team_id.split(":", 1)
    return SimpleNamespace(
        id=team_id,
        team_id=raw_id,
        league_id=league_id,
        name_long="Dallas Cowboys",
        name_medium="Cowboys",
        name_short="DAL",
        logo_url=None,  # column is never written — should not appear in output
    )


def test_team_dto_logo_url_uses_keyless_endpoint():
    team = _fake_team("NFL:DAL")
    dto = _team_dto(team, public_base="https://agg.example.com")
    assert dto is not None
    assert dto["logo_url"] == "https://agg.example.com/v1/teams/NFL:DAL/logo"


def test_team_dto_logo_url_relative_when_empty_base():
    team = _fake_team("NFL:DAL")
    dto = _team_dto(team, public_base="")
    assert dto is not None
    assert dto["logo_url"] == "/v1/teams/NFL:DAL/logo"


def test_team_dto_none_returns_none():
    assert _team_dto(None) is None


def test_team_dto_logo_url_ends_with_team_logo_path():
    team = _fake_team("NBA:LAL")
    dto = _team_dto(team, public_base="https://agg.example.com")
    assert dto["logo_url"].endswith("/v1/teams/NBA:LAL/logo")
