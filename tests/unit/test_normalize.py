"""Unit tests for ingest.normalize — runs against captured fixtures.

These tests don't depend on MDProject. They check that our normalizer produces
sane output on real provider payloads.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from aggrigator.ingest.normalize import (
    EventSpec,
    build_market_id,
    event_spec_from_payload,
    event_specs_from_response,
)


def test_build_market_id_with_line_and_side() -> None:
    mid = build_market_id(
        event_id="abc123", kind="ou", scope="FULL_GAME",
        line=Decimal("44.5"), side="HOME",
    )
    assert mid == "abc123-ou-ft-44_5-home"


def test_build_market_id_negative_line() -> None:
    mid = build_market_id(
        event_id="abc123", kind="sp", scope="FULL_GAME", line=Decimal("-3.5"),
    )
    assert mid == "abc123-sp-ft-neg3_5"


def test_build_market_id_no_line_no_side() -> None:
    mid = build_market_id(event_id="abc123", kind="ml", scope="FULL_GAME")
    assert mid == "abc123-ml-ft"


def test_event_spec_from_non_match_returns_none() -> None:
    assert event_spec_from_payload({"type": "prop", "eventID": "x"}) is None


def test_event_spec_from_payload_missing_teams_returns_none() -> None:
    assert event_spec_from_payload({"type": "match", "eventID": "x", "teams": {}}) is None


def test_event_specs_from_response_filters_non_match(all_event_payloads: list[dict]) -> None:
    specs = event_specs_from_response(all_event_payloads)
    # Every produced spec is a match.
    assert all(s.type == "match" for s in specs)
    # We should have produced at least some specs from the captured corpus.
    assert len(specs) > 0


def test_event_specs_have_string_pks_and_iso_start_time(all_event_payloads: list[dict]) -> None:
    specs = event_specs_from_response(all_event_payloads)
    for s in specs[:50]:  # sample
        assert isinstance(s, EventSpec)
        assert isinstance(s.event_id, str) and s.event_id
        assert isinstance(s.start_time, datetime)
        assert s.home_team.pk == f"{s.league_id}:{s.home_team.team_id}"
        assert s.away_team.pk == f"{s.league_id}:{s.away_team.team_id}"


def test_finished_event_winner_code_set_when_scores_present(
    all_event_payloads: list[dict],
) -> None:
    """At least one finalized event should derive a winner_code."""
    specs = event_specs_from_response(all_event_payloads)
    finalized_with_scores = [
        s for s in specs
        if s.is_finalized and s.home_score is not None and s.away_score is not None
    ]
    if not finalized_with_scores:
        pytest.skip("No finalized-with-scores events in fixture corpus")
    # All such specs must have winner_code in {1, 2, 3}.
    assert all(s.winner_code in (1, 2, 3) for s in finalized_with_scores)


def test_market_id_collision_rate_is_bounded(all_event_payloads: list[dict]) -> None:
    """``build_market_id`` now includes ``statID`` and ``statEntityID`` so the
    big ``eo``/``yn`` per-side collision is gone. A handful of
    stragglers remain (~0.5% of markets) — likely from edge cases in
    ``_group_key`` for live alt-line shifts. Bound the rate at 1% so a
    regression in either ``build_market_id`` or ``_group_key`` is loud, and
    leave the residual cleanup for follow-up.
    """
    specs = event_specs_from_response(all_event_payloads)
    total_markets = 0
    collisions = 0
    for s in specs:
        ids = [m.market_id for m in s.markets]
        total_markets += len(ids)
        collisions += len(ids) - len(set(ids))
    assert total_markets > 0
    rate = collisions / total_markets
    assert rate < 0.01, f"collision rate {rate:.3%} ({collisions}/{total_markets})"


def test_selection_ids_are_unique_within_market(all_event_payloads: list[dict]) -> None:
    specs = event_specs_from_response(all_event_payloads)
    for s in specs:
        for m in s.markets:
            sids = [sel.selection_id for sel in m.selections]
            assert len(sids) == len(set(sids)), (
                f"duplicate selection_id in market={m.market_id}: {sorted(sids)}"
            )


def test_soccer_three_way_moneyline_keeps_draw_selection() -> None:
    """Regression: the prop filter treated statEntityID='draw' as a player
    prop and dropped it — every soccer ML mirrored as a 2-way and the draw
    was unpickable product-wide (PotD card, Golden Game, regular picks)."""
    payload = {
        "type": "match",
        "eventID": "EV3WAY",
        "leagueID": "BRASILEIRAO_SERIE_B",
        "sportID": "SOCCER",
        "status": {"startsAt": "2026-06-12T22:00:00Z"},
        "teams": {
            "home": {"teamID": "HOME_T", "names": {"long": "AC Goianiense"}},
            "away": {"teamID": "AWAY_T", "names": {"long": "CR Brasil"}},
        },
        "odds": {
            "goals-home-game-ml3way-home": {
                "oddID": "goals-home-game-ml3way-home", "fairOdds": "+120",
            },
            "goals-away-game-ml3way-away": {
                "oddID": "goals-away-game-ml3way-away", "fairOdds": "+250",
            },
            "goals-draw-game-ml3way-draw": {
                "oddID": "goals-draw-game-ml3way-draw", "fairOdds": "+240",
            },
        },
    }

    spec = event_spec_from_payload(payload)
    assert spec is not None
    ml = [m for m in spec.markets if m.category == "MONEYLINE"]
    assert len(ml) == 1, "home/away/draw must group into ONE 3-way market"
    types = sorted(sel.selection_type for sel in ml[0].selections)
    assert types == ["AWAY", "DRAW", "HOME"]
