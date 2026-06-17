"""team_logo_url builds a link to this service's keyless logo endpoint."""

from __future__ import annotations

from aggrigator.schemas.team import team_logo_url


def test_relative_logo_url_when_no_public_base():
    assert team_logo_url("usa-nba:38", public_base="") == "/v1/teams/usa-nba:38/logo"


def test_absolute_logo_url_with_public_base():
    assert (
        team_logo_url("usa-nba:38", public_base="https://agg.example.com")
        == "https://agg.example.com/v1/teams/usa-nba:38/logo"
    )
