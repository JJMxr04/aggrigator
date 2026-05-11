"""Unit tests for ``odds_api_translate``.

Synthetic JSON in odds-api.io's documented shape → SGO-shape payload →
``event_spec_from_payload`` reparse → assertions on the resulting
``EventSpec``. Confirms the round trip: every existing downstream code
path (normalize, upsert, settlement, webhooks) sees identical structure
regardless of provider.

Real captured payloads land in ``tests/fixtures/oddsapi/`` after Phase 0
probe; for now we test against hand-crafted JSON.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aggrigator.ingest.normalize import event_spec_from_payload
from aggrigator.ingest.odds_api_errors import SchemaError
from aggrigator.ingest.odds_api_translate import (
    SGO_TO_ODDSAPI_LEAGUE,
    SGO_TO_ODDSAPI_SPORT,
    to_sgo_event_payload,
)


# ---- fixture factories ------------------------------------------------------


def _mlb_event_pending(
    event_id: int = 100,
    home: str = "Yankees",
    away: str = "Red Sox",
) -> dict:
    return {
        "id": event_id,
        "home": home,
        "away": away,
        "homeId": 1,
        "awayId": 2,
        "date": "2026-05-10T19:00:00Z",
        "status": "pending",
        "sport": {"name": "Baseball", "slug": "baseball"},
        "league": {"name": "USA - MLB", "slug": "usa-mlb"},
        "scores": {},
    }


def _mlb_event_settled(
    event_id: int = 100, home_score: int = 5, away_score: int = 3,
) -> dict:
    return {
        "id": event_id,
        "home": "Yankees", "away": "Red Sox", "homeId": 1, "awayId": 2,
        "date": "2026-05-09T19:00:00Z",
        "status": "settled",
        "sport": {"name": "Baseball", "slug": "baseball"},
        "league": {"name": "USA - MLB", "slug": "usa-mlb"},
        "scores": {
            "home": home_score, "away": away_score,
            "periods": {"fulltime": {"home": home_score, "away": away_score}},
        },
    }


def _mlb_odds_payload(
    event_id: int = 100,
) -> dict:
    """One bookmaker (Bet365) with ML + AH + Totals — main lines only."""
    return {
        "id": event_id,
        "home": "Yankees", "away": "Red Sox",
        "date": "2026-05-10T19:00:00Z", "status": "pending",
        "sport": {"name": "Baseball", "slug": "baseball"},
        "league": {"name": "USA - MLB", "slug": "usa-mlb"},
        "urls": {"Bet365": "https://bet365.example/123"},
        "bookmakers": {
            "Bet365": [
                {
                    "name": "ML",
                    "updatedAt": "2026-05-10T17:00:00Z",
                    "odds": [{"home": "1.85", "away": "2.10"}],
                },
                {
                    "name": "Asian Handicap",
                    "updatedAt": "2026-05-10T17:00:00Z",
                    "odds": [
                        {"hdp": -1.5, "home": "2.20", "away": "1.75"},
                        {"hdp": 0,    "home": "1.80", "away": "2.10"},  # main
                        {"hdp": 1.5,  "home": "1.55", "away": "2.45"},
                    ],
                },
                {
                    "name": "Totals",
                    "updatedAt": "2026-05-10T17:00:00Z",
                    "odds": [
                        {"hdp": 7.5, "over": "1.70", "under": "2.20"},
                        {"hdp": 8.5, "over": "1.90", "under": "1.90"},  # main
                        {"hdp": 9.5, "over": "2.20", "under": "1.70"},
                    ],
                },
                # Player-props market — must be dropped at the boundary.
                {
                    "name": "Player Props",
                    "updatedAt": "2026-05-10T17:00:00Z",
                    "odds": [{"home": "9.00", "away": "1.10"}],
                },
            ],
        },
    }


# ---- live → dropped ---------------------------------------------------------


def test_live_event_dropped() -> None:
    ev = _mlb_event_pending()
    ev["status"] = "live"
    assert to_sgo_event_payload(ev) is None


# ---- settled → SGO payload ---------------------------------------------------


def test_settled_event_translated_with_scores() -> None:
    ev = _mlb_event_settled(home_score=5, away_score=3)
    payload = to_sgo_event_payload(ev)
    assert payload is not None
    assert payload["type"] == "match"
    assert payload["eventID"] == "100"
    assert payload["sportID"] == "BASEBALL"
    assert payload["leagueID"] == "MLB"
    assert payload["status"]["finalized"] is True
    assert payload["status"]["cancelled"] is False
    assert payload["odds"] == {}  # no /odds call on settled
    assert payload["teams"]["home"]["score"] == 5
    assert payload["teams"]["away"]["score"] == 3

    # Reparse with normalize → EventSpec round-trip
    spec = event_spec_from_payload(payload)
    assert spec is not None
    assert spec.event_id == "100"
    assert spec.sport_id == "BASEBALL"
    assert spec.is_finalized is True
    assert spec.status_type == "finished"
    assert spec.home_score == 5
    assert spec.away_score == 3
    assert spec.winner_code == 1  # home wins


def test_settled_with_null_scores_becomes_cancelled() -> None:
    """cancelled-suspended.md §4.1 — settled+null-scores = abandoned."""
    ev = _mlb_event_settled()
    ev["scores"] = {"home": None, "away": None}
    payload = to_sgo_event_payload(ev)
    assert payload is not None
    assert payload["status"]["cancelled"] is True
    assert payload["status"]["finalized"] is False

    spec = event_spec_from_payload(payload)
    assert spec is not None
    assert spec.status_type == "canceled"
    assert spec.feed_locked is True


# ---- pending → SGO payload with odds ----------------------------------------


def test_pending_event_with_odds_translates_main_lines() -> None:
    ev = _mlb_event_pending()
    odds = _mlb_odds_payload()
    payload = to_sgo_event_payload(ev, odds)
    assert payload is not None
    assert payload["status"]["finalized"] is False
    assert payload["status"]["cancelled"] is False

    odd_keys = set(payload["odds"].keys())
    # ML — 2-way (no draw in baseball)
    assert "runs-home-game-ml-home" in odd_keys
    assert "runs-away-game-ml-away" in odd_keys
    # AH main line (hdp=0)
    assert "runs-home-game-sp-home" in odd_keys
    assert "runs-away-game-sp-away" in odd_keys
    # Totals main line (hdp closest to MLB canonical 8.5)
    assert "runs-all-game-ou-over" in odd_keys
    assert "runs-all-game-ou-under" in odd_keys
    # Alt-line and player-prop markets must be dropped.
    assert not any("9_5" in k or "7_5" in k for k in odd_keys)


def test_pending_event_picks_main_totals_line_closest_to_canonical() -> None:
    ev = _mlb_event_pending()
    odds = _mlb_odds_payload()
    payload = to_sgo_event_payload(ev, odds)
    assert payload is not None
    # MLB canonical = 8.5, so over/under entries should reflect that line.
    over_row = payload["odds"]["runs-all-game-ou-over"]
    assert over_row.get("fairOverUnder") == "8.5"


def test_pending_event_empty_bookmakers_becomes_event_only() -> None:
    """cancelled-suspended.md §4.2 — pending+no books = WebSocket no_markets."""
    ev = _mlb_event_pending()
    payload = to_sgo_event_payload(ev, {"bookmakers": {}})
    assert payload is not None
    assert payload["odds"] == {}  # no quotes preserved


def test_no_odds_dict_argument_yields_event_only() -> None:
    ev = _mlb_event_pending()
    payload = to_sgo_event_payload(ev, None)
    assert payload is not None
    assert payload["odds"] == {}


# ---- soccer ML3way ----------------------------------------------------------


def test_soccer_ml_three_way_emits_draw_selection() -> None:
    ev = {
        "id": 200, "home": "Arsenal", "away": "Chelsea",
        "homeId": 10, "awayId": 11, "date": "2026-05-10T15:00:00Z",
        "status": "pending",
        "sport": {"name": "Football", "slug": "football"},
        "league": {"name": "EPL", "slug": "england-premier-league"},
        "scores": {},
    }
    odds = {
        "id": 200, "home": "Arsenal", "away": "Chelsea",
        "date": "2026-05-10T15:00:00Z", "status": "pending",
        "sport": {"name": "Football", "slug": "football"},
        "league": {"name": "EPL", "slug": "england-premier-league"},
        "urls": {"Bet365": ""},
        "bookmakers": {
            "Bet365": [
                {
                    "name": "ML",
                    "updatedAt": "2026-05-10T13:00:00Z",
                    "odds": [{"home": "2.10", "draw": "3.40", "away": "3.20"}],
                },
            ],
        },
    }
    payload = to_sgo_event_payload(ev, odds)
    assert payload is not None
    odd_keys = set(payload["odds"].keys())
    # Soccer = goals stat, ml3way bet type
    assert "goals-home-game-ml3way-home" in odd_keys
    assert "goals-away-game-ml3way-away" in odd_keys
    assert "goals-draw-game-ml3way-draw" in odd_keys


# ---- bookmaker filtering / per-book quotes ----------------------------------


def test_pending_event_per_bookmaker_quotes_normalized_to_lowercase() -> None:
    ev = _mlb_event_pending()
    odds = _mlb_odds_payload()
    payload = to_sgo_event_payload(ev, odds)
    assert payload is not None
    ml_home = payload["odds"]["runs-home-game-ml-home"]
    # Bet365 display name normalized to "bet365" internal ID.
    assert "bet365" in ml_home["byBookmaker"]
    assert "Bet365" not in ml_home["byBookmaker"]


# ---- schema guards ----------------------------------------------------------


def test_missing_required_field_raises_schema_error() -> None:
    ev = {"id": 1, "status": "pending"}  # missing home/away/date
    with pytest.raises(SchemaError):
        to_sgo_event_payload(ev)


def test_unknown_sport_slug_drops_event() -> None:
    ev = _mlb_event_pending()
    ev["sport"] = {"name": "Cricket", "slug": "cricket"}
    assert to_sgo_event_payload(ev) is None


def test_unknown_league_slug_drops_event() -> None:
    ev = _mlb_event_pending()
    ev["league"] = {"name": "??", "slug": "usa-fakelg"}
    assert to_sgo_event_payload(ev) is None


# ---- slug maps are self-consistent ------------------------------------------


def test_slug_maps_are_bijective() -> None:
    """Defensive: SGO→odds-api maps and odds-api→SGO maps should agree on
    every entry. Catches typos when extending the dicts."""
    from aggrigator.ingest.odds_api_translate import (
        ODDSAPI_TO_SGO_LEAGUE,
        ODDSAPI_TO_SGO_SPORT,
    )
    for sgo, slug in SGO_TO_ODDSAPI_SPORT.items():
        assert ODDSAPI_TO_SGO_SPORT[slug] == sgo
    for sgo, slug in SGO_TO_ODDSAPI_LEAGUE.items():
        assert ODDSAPI_TO_SGO_LEAGUE[slug] == sgo
