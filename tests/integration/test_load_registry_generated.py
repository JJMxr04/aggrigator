"""After seeding, load_registry must validate + apply the on-disk registry
without error and annotate odds_api_io_key on rows.

The on-disk SOCCER-leagues.json currently holds odds_api_io_key == "EPL"
(the canonical id) because the live generator has not yet been run.  After
the operator runs generate_provider_catalog.py the value becomes the provider
slug ("england-premier-league") and this test still holds because it reads the
expected value directly from the file rather than hard-coding it.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from sqlalchemy import select

from aggrigator.ingest.seed import seed_leagues
from aggrigator.models import League
from aggrigator.schemas.registry import key_or_none
from aggrigator.workers.tasks.load_registry import run_load_registry
from tests.integration.factories import make_sport

pytestmark = pytest.mark.asyncio

_SOCCER_LEAGUES_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "aggrigator"
    / "data"
    / "sports"
    / "SOCCER"
    / "leagues"
    / "SOCCER-leagues.json"
)


def _on_disk_epl_odds_api_key() -> str | None:
    """Parse the on-disk SOCCER-leagues.json and return the EPL odds_api_io_key
    converted through key_or_none semantics (false JSON sentinel -> None)."""
    data = json.loads(_SOCCER_LEAGUES_PATH.read_text())
    for entry in data["leagues"]:
        if entry["canonical_id"] == "EPL":
            raw = entry["odds_api_io_key"]
            # key_or_none converts JSON false / "" to None; strings pass through.
            return key_or_none(raw)
    raise RuntimeError("EPL not found in on-disk SOCCER-leagues.json")


async def test_load_registry_applies_after_seed(session) -> None:
    # Seed SOCCER sport (active) and the EPL league so load_registry has a
    # DB row to annotate.  run_load_registry() processes all sports in
    # sports.json; missing rows land in report.issues (not raised), so we only
    # need to seed what we want to assert against.
    await make_sport(session, id="SOCCER", name="Soccer", active=True)
    await seed_leagues(
        session,
        None,
        _payloads=[
            {
                "leagueID": "EPL",
                "sportID": "SOCCER",
                "name": "EPL",
                "shortName": "EPL",
            }
        ],
    )
    # Commit so session_scope()'s independent session sees the rows.
    await session.commit()

    # run_load_registry() opens its own session via session_scope() and commits.
    # A raised exception means validation failed — let it propagate as a test failure.
    report = await run_load_registry()

    # The returned dict must be non-empty (at minimum leagues_updated > 0).
    assert report, f"run_load_registry returned empty report: {report!r}"

    # session_scope() committed its own transaction; roll back our open
    # transaction so the connection re-reads the committed state.
    await session.rollback()

    epl = (await session.scalars(select(League).where(League.id == "EPL"))).one()

    expected_key = _on_disk_epl_odds_api_key()
    assert epl.odds_api_io_key == expected_key, (
        f"EPL.odds_api_io_key expected {expected_key!r} "
        f"(from on-disk SOCCER-leagues.json), got {epl.odds_api_io_key!r}"
    )
