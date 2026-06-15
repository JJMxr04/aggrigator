"""End-to-end ingest: FixtureOddsClient -> orchestrator -> DB rows."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from aggrigator.ingest.client import OddsClient
from aggrigator.ingest.lifecycle import Transition
from aggrigator.ingest.orchestrator import ingest_event, ingest_league
from aggrigator.models import (
    Bookmaker,
    BookmakerSelection,
    Event,
    Market,
    OddsQuote,
    Selection,
    Team,
)
from tests.integration.factories import make_league, make_sport

pytestmark = pytest.mark.asyncio


async def _seed_league(session, sport_id: str, league_id: str):
    sport = await make_sport(session, id=sport_id, name=sport_id.title())
    return await make_league(session, sport=sport, id=league_id, name=league_id, active=True)


def _first_match_event(client: OddsClient, league_id: str) -> dict:
    """The first event in a league fixture isn't always ``type="match"``
    (Pro Bowl skills events ship as ``type="prop"`` and the normalizer drops
    them). Walk until we find a match-type event so the tests run against
    something the ingester will actually process."""
    for ev in client.get_events(league_id=league_id):
        if ev.get("type") == "match":
            return ev
    raise RuntimeError(f"no match-type event in {league_id} fixture")


async def test_ingest_one_nfl_event(session, fixture_odds_client: OddsClient) -> None:
    await _seed_league(session, "FOOTBALL", "NFL")

    payload = _first_match_event(fixture_odds_client, "NFL")
    result = await ingest_event(session, payload)
    assert result is not None
    await session.commit()

    fetched = await session.get(Event, result.event.id)
    assert fetched is not None
    assert fetched.league_id == "NFL"
    assert fetched.sport_id == "FOOTBALL"

    markets = list(await session.scalars(
        select(Market).where(Market.event_id == result.event.id)
    ))
    assert markets, "expected at least one market"

    selections = list(await session.scalars(
        select(Selection).where(Selection.market_id.in_([m.id for m in markets]))
    ))
    assert selections, "expected at least one selection"


async def test_ingest_skips_event_when_league_not_seeded(
    session, fixture_odds_client: OddsClient,
) -> None:
    """Without a League row the ingester refuses to corrupt with a half-event."""
    payload = _first_match_event(fixture_odds_client, "NFL")
    result = await ingest_event(session, payload)
    assert result is None
    await session.commit()

    count = await session.scalar(
        select(Event).where(Event.id == payload["eventID"])
    )
    assert count is None


async def test_ingest_auto_creates_teams_for_unrostered_league(
    session, fixture_odds_client: OddsClient,
) -> None:
    """Phase 16 onboarding: with the league seeded but NO team roster, ingesting
    an event auto-creates its home/away Team rows via ``upsert_team_from_spec``
    — no registry/roster step needed (that was the TheSportsDB hassle we retired).
    """
    await _seed_league(session, "FOOTBALL", "NFL")
    assert await session.scalar(select(func.count()).select_from(Team)) == 0

    payload = _first_match_event(fixture_odds_client, "NFL")
    result = await ingest_event(session, payload)
    assert result is not None
    await session.commit()

    teams = list(await session.scalars(select(Team).where(Team.league_id == "NFL")))
    assert len(teams) == 2  # home + away auto-created on first sight
    ev = await session.get(Event, result.event.id)
    assert ev.home_team_id and ev.away_team_id


async def test_re_ingest_is_idempotent(session, fixture_odds_client: OddsClient) -> None:
    """Same payload twice -> no duplicate rows."""
    await _seed_league(session, "FOOTBALL", "NFL")
    payload = _first_match_event(fixture_odds_client, "NFL")

    await ingest_event(session, payload)
    await session.commit()
    counts_after_first = {
        "events": await session.scalar(select(Event).where(Event.id == payload["eventID"])),
        "markets": len(list(await session.scalars(
            select(Market).where(Market.event_id == payload["eventID"])
        ))),
        "selections": len(list(await session.scalars(
            select(Selection).join(Market).where(Market.event_id == payload["eventID"])
        ))),
    }

    await ingest_event(session, payload)
    await session.commit()
    counts_after_second = {
        "events": await session.scalar(select(Event).where(Event.id == payload["eventID"])),
        "markets": len(list(await session.scalars(
            select(Market).where(Market.event_id == payload["eventID"])
        ))),
        "selections": len(list(await session.scalars(
            select(Selection).join(Market).where(Market.event_id == payload["eventID"])
        ))),
    }

    assert counts_after_first["markets"] == counts_after_second["markets"]
    assert counts_after_first["selections"] == counts_after_second["selections"]


async def test_first_ingest_returns_lifecycle_transition(
    session, fixture_odds_client: OddsClient,
) -> None:
    await _seed_league(session, "FOOTBALL", "NFL")
    payload = _first_match_event(fixture_odds_client, "NFL")
    result = await ingest_event(session, payload)
    await session.commit()
    assert result is not None
    # First time we see the event, regardless of its current status.
    assert result.transition in (
        Transition.LIFECYCLE_CHANGED,
        Transition.FINALIZED,
        Transition.VOIDED,
    )


async def test_re_ingest_unchanged_returns_none_transition(
    session, fixture_odds_client: OddsClient,
) -> None:
    await _seed_league(session, "FOOTBALL", "NFL")
    payload = _first_match_event(fixture_odds_client, "NFL")
    await ingest_event(session, payload)
    await session.commit()
    result = await ingest_event(session, payload)
    await session.commit()
    assert result is not None
    assert result.transition == Transition.NONE


async def test_ingest_league_walks_corpus(session, fixture_odds_client: OddsClient) -> None:
    league = await _seed_league(session, "FOOTBALL", "NFL")
    report = await ingest_league(session, league, fixture_odds_client)
    await session.commit()

    assert report.events_processed > 0, "fixture corpus should have NFL events"
    assert report.events_failed == 0
    # Every processed event yielded *some* transition (even NONE on no-op).
    assert len(report.transitions) == report.events_processed
    # last_refreshed_at written on the league row.
    assert league.last_refreshed_at is not None


async def test_finalized_event_records_quotes(
    session, fixture_odds_client: OddsClient,
) -> None:
    """If the fixture has any finalized event, ingesting it must populate
    Selection.decimal_odds (denormalized) and at least one OddsQuote row."""
    await _seed_league(session, "FOOTBALL", "NFL")
    finalized = None
    for ev in fixture_odds_client.get_events(league_id="NFL"):
        if (ev.get("status") or {}).get("finalized"):
            finalized = ev
            break
    if finalized is None:
        pytest.skip("no finalized NFL events in fixture corpus")

    result = await ingest_event(session, finalized)
    await session.commit()
    assert result is not None

    quotes = list(await session.scalars(
        select(OddsQuote).join(Selection).join(Market).where(Market.event_id == result.event.id)
    ))
    assert quotes, "ingest of finalized event should write at least one OddsQuote"


async def test_bookmakers_seed_on_demand(
    session, fixture_odds_client: OddsClient,
) -> None:
    """Per-book quotes auto-create the Bookmaker row if missing."""
    await _seed_league(session, "FOOTBALL", "NFL")
    payload = _first_match_event(fixture_odds_client, "NFL")
    await ingest_event(session, payload)
    await session.commit()

    book_quotes = list(await session.scalars(select(BookmakerSelection)))
    if not book_quotes:
        pytest.skip("event had no per-bookmaker odds")
    books = list(await session.scalars(select(Bookmaker)))
    assert books, "Bookmaker row must be auto-created on first per-book quote"
