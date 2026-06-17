"""TeamView's logo formatter renders an <img> at the keyless logo endpoint."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from markupsafe import Markup

from aggrigator.admin import views


def _fake_team(team_id: str = "usa-nba:38") -> SimpleNamespace:
    return SimpleNamespace(id=team_id)


def test_logo_thumb_absolute_base():
    with mock.patch.object(
        views, "get_settings",
        return_value=SimpleNamespace(public_base_url="https://agg.example.com"),
    ):
        out = views._team_logo_thumb(_fake_team("usa-nba:38"), "logo")
    assert isinstance(out, Markup)
    assert 'src="https://agg.example.com/v1/teams/usa-nba:38/logo"' in str(out)
    assert 'height="24"' in str(out)
    assert str(out).startswith("<img")


def test_logo_thumb_relative_when_base_empty():
    with mock.patch.object(
        views, "get_settings",
        return_value=SimpleNamespace(public_base_url=""),
    ):
        out = views._team_logo_thumb(_fake_team("usa-nba:38"), "logo")
    assert 'src="/v1/teams/usa-nba:38/logo"' in str(out)


def test_logo_thumb_escapes_dangerous_id():
    with mock.patch.object(
        views, "get_settings",
        return_value=SimpleNamespace(public_base_url=""),
    ):
        out = views._team_logo_thumb(_fake_team('x":onerror=alert(1)//'), "logo")
    # The raw double-quote from the id must NOT appear unescaped inside the tag
    # (it would terminate the src attribute). markupsafe escapes it to &#34;.
    assert '"x"' not in str(out)
    assert "&#34;" in str(out) or "&quot;" in str(out)


def test_team_view_renders_logo_in_list_and_detail():
    # List view: synthetic "logo" column, first.
    assert views.TeamView.column_list[0] == "logo"
    assert views.TeamView.column_formatters["logo"] is views._team_logo_thumb
    # Detail view: the real (dormant) logo_url column is rendered as a thumb,
    # since a synthetic column can't appear in the model's default detail list.
    assert views.TeamView.column_formatters_detail["logo_url"] is views._team_logo_thumb


def test_team_view_logo_url_not_editable_in_form():
    # The dormant/computed logo_url is excluded from the edit form so the
    # operator can't type into a field that's synthesized at serialization.
    assert "logo_url" in views.TeamView.form_excluded_columns
