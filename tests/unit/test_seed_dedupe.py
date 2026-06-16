"""Seed payload de-duplication.

odds-api's ``/leagues`` carries the season phase in the slug, so during a
phase-overlap window it returns both ``usa-nba`` and ``usa-nba-playoffs`` —
and ``match_league_slug`` collapses BOTH to internal leagueID ``NBA`` (see
aggrigator.ingest.odds_api_translate). ``get_leagues`` therefore emits the
same ``leagueID`` twice. The seed session runs with ``autoflush=False``, so a
naive loop would ``session.add`` two pending ``League(id="NBA")`` rows and die
on the ``pk_core_league`` unique constraint at flush. The seed must collapse
duplicates to a single payload (first wins) before the upsert loop.
"""

from __future__ import annotations

from aggrigator.ingest.seed import _dedupe_payloads


def test_collapses_repeated_key_first_wins():
    payloads = [
        {"leagueID": "NBA", "name": "NBA"},
        {"leagueID": "NBA", "name": "NBA Playoffs"},
        {"leagueID": "MLB", "name": "MLB"},
    ]
    deduped, dropped = _dedupe_payloads(payloads, "leagueID")
    assert [p["leagueID"] for p in deduped] == ["NBA", "MLB"]
    assert deduped[0]["name"] == "NBA"  # first occurrence wins
    assert dropped == 1


def test_no_duplicates_is_passthrough():
    payloads = [{"sportID": "BASKETBALL"}, {"sportID": "BASEBALL"}]
    deduped, dropped = _dedupe_payloads(payloads, "sportID")
    assert deduped == payloads
    assert dropped == 0


def test_missing_key_rows_pass_through_and_are_not_deduped():
    # Rows without the key are left for the upsert helper to skip; two of them
    # are NOT collapsed into one (they aren't duplicate PKs — they're no-PKs).
    payloads = [{"name": "x"}, {"name": "y"}, {"leagueID": "NFL"}]
    deduped, dropped = _dedupe_payloads(payloads, "leagueID")
    assert deduped == payloads
    assert dropped == 0


def test_empty_list():
    assert _dedupe_payloads([], "leagueID") == ([], 0)
