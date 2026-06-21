import pytest

from aggrigator.ingest.catalog_build import (
    strip_phase, derive_canonical_id, build_league_pattern, build_catalog,
)


def test_strip_phase_removes_known_suffix():
    assert strip_phase("usa-nba-playoffs") == "usa-nba"
    assert strip_phase("usa-nfl-preseason") == "usa-nfl"


def test_strip_phase_leaves_non_phase_suffix():
    # "women"/"apertura"/"next-pro" are NOT phases — they are distinct leagues.
    assert strip_phase("international-nations-league-women") == "international-nations-league-women"
    assert strip_phase("usa-mls-next-pro") == "usa-mls-next-pro"


def test_derive_canonical_id_basic():
    assert derive_canonical_id("argentina-primera-b") == "ARGENTINA_PRIMERA_B"


def test_derive_canonical_id_truncates_with_hash_and_stays_under_48():
    long = "a-very-long-league-slug-" + "x" * 60
    cid = derive_canonical_id(long, maxlen=48)
    assert len(cid) <= 48
    # deterministic: same input -> same output
    assert cid == derive_canonical_id(long, maxlen=48)


def test_build_league_pattern_matches_phase_variants():
    pat = build_league_pattern("usa-nba")
    assert pat.match("usa-nba")
    assert pat.match("usa-nba-playoffs")
    # anchored: must not swallow a different league sharing the prefix
    assert pat.match("usa-nba-summer-league") is None


def test_build_catalog_preserves_existing_canonical_id():
    sports = [{"slug": "baseball", "name": "Baseball"}]
    leagues_by_sport = {"baseball": [
        {"slug": "usa-mlb", "name": "USA - MLB"},
        {"slug": "usa-mlb-playoffs", "name": "USA - MLB Playoffs"},
        {"slug": "japan-npb", "name": "Japan - NPB"},
    ]}
    existing = {
        "sport_slugs": {"BASEBALL": "baseball"},
        "league_slugs": {"MLB": "usa-mlb"},
    }
    cat = build_catalog(sports, leagues_by_sport, existing)
    # known league keeps canonical id; phase variant collapses into it
    assert cat.league_slugs["MLB"] == "usa-mlb"
    assert "USA_MLB" not in cat.league_slugs
    # new league derives a fresh id
    assert cat.league_slugs["JAPAN_NPB"] == "japan-npb"
    # sport mapping preserved
    assert cat.sport_slugs["BASEBALL"] == "baseball"
    # reverse linkage present
    assert cat.league_to_sport["MLB"] == "BASEBALL"
    assert cat.league_to_sport["JAPAN_NPB"] == "BASEBALL"


def test_seed_catalog_matches_legacy_maps():
    from aggrigator.ingest import odds_api_catalog as cat
    # Today's known mappings must be present and unchanged.
    assert cat.SPORT_SLUGS["SOCCER"] == "football"
    assert cat.SPORT_SLUGS["FOOTBALL"] == "american-football"
    assert cat.LEAGUE_SLUGS["MLB"] == "usa-mlb"
    assert cat.LEAGUE_SLUGS["EPL"] == "england-premier-league"
    assert cat.LEAGUE_TO_SPORT["NBA"] == "BASKETBALL"
    assert cat.LEAGUE_TO_SPORT["VNL_WOMEN"] == "VOLLEYBALL"


def test_build_catalog_preserves_existing_league_to_sport_without_slug():
    # CEBL has no provider slug — it lives only in league_to_sport and must
    # survive a regeneration that doesn't include it in the provider listing.
    sports = [{"slug": "basketball", "name": "Basketball"}]
    leagues_by_sport = {"basketball": [
        {"slug": "usa-nba", "name": "USA - NBA"},
    ]}
    existing = {
        "sport_slugs": {"BASKETBALL": "basketball"},
        "league_slugs": {},
        "league_to_sport": {"CEBL": "BASKETBALL"},
    }
    cat = build_catalog(sports, leagues_by_sport, existing)
    # Curated entry must be present in league_to_sport ...
    assert cat.league_to_sport["CEBL"] == "BASKETBALL"
    # ... but must NOT appear in league_slugs (it has no provider slug).
    assert "CEBL" not in cat.league_slugs


def test_build_catalog_preserves_offseason_league_slug():
    # A league absent from this run's provider listing must keep its slug and
    # sport mapping from the existing catalog (off-season / provider doesn't
    # return it this cycle).
    sports = [{"slug": "basketball", "name": "Basketball"}]
    # Provider returns a basketball league that is NOT any nba* slug.
    leagues_by_sport = {"basketball": [
        {"slug": "spain-acb", "name": "Spain - ACB"},
    ]}
    existing = {
        "sport_slugs": {"BASKETBALL": "basketball"},
        "league_slugs": {"NBA": "usa-nba"},
        "league_to_sport": {"NBA": "BASKETBALL"},
    }
    cat = build_catalog(sports, leagues_by_sport, existing)
    # Off-season NBA survives.
    assert cat.league_slugs["NBA"] == "usa-nba"
    assert cat.league_to_sport["NBA"] == "BASKETBALL"
    # New in-season league also present.
    assert "SPAIN_ACB" in cat.league_slugs

    # Phase variant of an existing league collapses onto the canonical id —
    # provider returning "usa-nba-playoffs" must not create a new USA_NBA_PLAYOFFS key.
    leagues_with_phase = {"basketball": [
        {"slug": "usa-nba-playoffs", "name": "USA - NBA Playoffs"},
    ]}
    cat2 = build_catalog(sports, leagues_with_phase, existing)
    assert "NBA" in cat2.league_slugs
    assert "USA_NBA_PLAYOFFS" not in cat2.league_slugs


def test_render_catalog_module_is_importable(tmp_path):
    from aggrigator.ingest.catalog_build import Catalog
    import importlib.util
    import sys
    import pathlib
    # Cross-repo contract: the renderer lives in the sibling sports-scores-sim
    # repo, present in the local PBL monorepo but not in aggrigator's own CI
    # checkout. Skip when it isn't on disk rather than fail.
    scripts_dir = (
        pathlib.Path(__file__).resolve().parents[2].parent / "sports-scores-sim" / "scripts"
    )
    if not (scripts_dir / "generate_provider_catalog.py").exists():
        pytest.skip("sibling sports-scores-sim repo not checked out (cross-repo contract test)")
    sys.path.insert(0, str(scripts_dir))
    from generate_provider_catalog import render_catalog_module
    cat = Catalog(
        sport_slugs={"BASEBALL": "baseball"},
        league_slugs={"MLB": "usa-mlb"},
        league_to_sport={"MLB": "BASEBALL"},
    )
    src = render_catalog_module(cat)
    path = tmp_path / "gen.py"
    path.write_text(src)
    spec = importlib.util.spec_from_file_location("gen", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.SPORT_SLUGS == {"BASEBALL": "baseball"}
    assert mod.LEAGUE_TO_SPORT == {"MLB": "BASEBALL"}
