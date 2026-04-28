"""Unit tests for webhooks.idempotency — deterministic state-keyed hashing."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from aggrigator.ingest.normalize import (
    EventSpec,
    MarketSpec,
    SelectionSpec,
    TeamSpec,
)
from aggrigator.webhooks.idempotency import (
    idempotency_key,
    state_blob_from_event,
    state_blob_from_specs,
    state_hash,
)


def _spec(*, status_type: str, home: int | None, away: int | None,
          selections: list[str]) -> EventSpec:
    home_team = TeamSpec(team_id="DAL", league_id="NFL", name_long="Dallas",
                         name_medium="Dallas", name_short="DAL")
    away_team = TeamSpec(team_id="PHI", league_id="NFL", name_long="Philly",
                         name_medium="Philly", name_short="PHI")
    market = MarketSpec(
        market_id="m1", event_id="evt1", league_id="NFL", sport_id="FOOTBALL",
        category="MONEYLINE", market_type="NFL_POINTS_ML", scope="FULL_GAME",
        line=None, side="", subject_team_pk=None, provider_market_id="",
        is_live=False, suspended=False,
        last_updated_at=datetime.now(tz=timezone.utc),
        selections=[
            SelectionSpec(
                selection_id=sid, market_id="m1", selection_type="HOME",
                label="x", decimal_odds=Decimal("1.5"),
                opening_decimal_odds=None, suspended=False,
                raw_side_id="home", raw_stat_entity_id="home", score=None,
            )
            for sid in selections
        ],
    )
    return EventSpec(
        event_id="evt1", sport_id="FOOTBALL", league_id="NFL", type="match",
        season_label="", start_time=datetime.now(tz=timezone.utc),
        status_type=status_type, status_display="", current_period_id="",
        is_live=False, is_finalized=False, completed=False,
        home_team=home_team, away_team=away_team,
        home_score=home, away_score=away, winner_code=None, feed_locked=False,
        markets=[market],
    )


def test_same_state_yields_same_blob() -> None:
    a = state_blob_from_specs(_spec(status_type="finished", home=27, away=24, selections=["s1", "s2"]))
    b = state_blob_from_specs(_spec(status_type="finished", home=27, away=24, selections=["s1", "s2"]))
    assert a == b


def test_different_status_yields_different_blob() -> None:
    a = state_blob_from_specs(_spec(status_type="inprogress", home=14, away=10, selections=["s1"]))
    b = state_blob_from_specs(_spec(status_type="finished", home=14, away=10, selections=["s1"]))
    assert a != b


def test_different_score_yields_different_blob() -> None:
    a = state_blob_from_specs(_spec(status_type="finished", home=27, away=24, selections=["s1"]))
    b = state_blob_from_specs(_spec(status_type="finished", home=28, away=24, selections=["s1"]))
    assert a != b


def test_selection_order_does_not_affect_blob() -> None:
    a = state_blob_from_specs(_spec(status_type="finished", home=10, away=10, selections=["s1", "s2"]))
    b = state_blob_from_specs(_spec(status_type="finished", home=10, away=10, selections=["s2", "s1"]))
    assert a == b


def test_state_hash_is_16_hex_chars() -> None:
    blob = state_blob_from_specs(_spec(status_type="finished", home=27, away=24, selections=["s1"]))
    h = state_hash(blob)
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


def test_idempotency_key_format() -> None:
    blob = state_blob_from_specs(_spec(status_type="finished", home=27, away=24, selections=["s1"]))
    key = idempotency_key("evt1", blob)
    assert key.startswith("evt1:")
    assert len(key.split(":")[1]) == 16


def test_state_blob_from_event_matches_specs_when_equivalent() -> None:
    """Both entry points produce the same bytes for equivalent state."""
    spec_blob = state_blob_from_specs(_spec(
        status_type="finished", home=27, away=24, selections=["s1", "s2"],
    ))
    db_blob = state_blob_from_event(
        status_type="finished",
        home_score=27, away_score=24,
        selection_states=[("s1", "PENDING"), ("s2", "PENDING")],
    )
    assert spec_blob == db_blob


def test_settled_state_diverges_from_pending_state() -> None:
    """After grading, the blob changes — so a re-fire on score correction
    produces a fresh idempotency key."""
    pending = state_blob_from_event(
        status_type="finished", home_score=27, away_score=24,
        selection_states=[("s1", "PENDING")],
    )
    won = state_blob_from_event(
        status_type="finished", home_score=27, away_score=24,
        selection_states=[("s1", "WON")],
    )
    assert pending != won
