"""Unit: TeamSyncOut trims provider/internal fields, keeps the 12 syncable ones."""

from __future__ import annotations

from types import SimpleNamespace

from aggrigator.schemas.team import TeamListOut, TeamSyncOut


def _team_like(**overrides):
    base = dict(
        id="NFL:DAL",
        public_id="11111111-1111-1111-1111-111111111111",
        league_id="NFL",
        team_id="DAL",
        sport_id="FOOTBALL",
        name_long="Dallas Cowboys",
        name_medium="Cowboys",
        name_short="DAL",
        primary_color="#003594",
        secondary_color="#869397",
        primary_contrast="#FFFFFF",
        secondary_contrast="#000000",
        stat_entity_id="home",
        odds_api_io_key="dal-oddsapi",
        thesportsdb_team_id="134934",
        match_confirmed=True,
        match_source="fuzzy_match",
        logo_url="/v1/teams/NFL:DAL/logo",
        canonical_name="Dallas Cowboys",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_team_sync_out_has_12_fields_and_excludes_internal():
    out = TeamSyncOut.model_validate(_team_like())
    dumped = out.model_dump()
    assert set(dumped) == {
        "id", "league_id", "team_id", "sport_id",
        "name_long", "name_medium", "name_short",
        "primary_color", "secondary_color",
        "primary_contrast", "secondary_contrast",
        "stat_entity_id",
    }
    assert "odds_api_io_key" not in dumped
    assert "thesportsdb_team_id" not in dumped
    assert "public_id" not in dumped
    assert "logo_url" not in dumped
    assert "match_confirmed" not in dumped


def test_team_sync_out_allows_null_colors_and_sport():
    out = TeamSyncOut.model_validate(
        _team_like(primary_color=None, secondary_color=None, sport_id=None)
    )
    assert out.primary_color is None
    assert out.sport_id is None


def test_team_list_out_envelope_shape():
    page = TeamListOut(items=[TeamSyncOut.model_validate(_team_like())],
                       page=1, page_size=200, pages=1, total=1)
    assert page.page == 1 and page.pages == 1 and page.total == 1
    assert len(page.items) == 1
