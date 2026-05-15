"""Shared fixtures.

Tests run against captured odds-api.io JSON under ``tests/fixtures/oddsapi/``.
Populate the directory with::

    ODDSAPI_API_KEY=... python -m aggrigator.scripts.capture_oddsapi_fixtures

Tests that need fixtures will ``pytest.skip`` until the capture runs.
"""

from __future__ import annotations

import os
from pathlib import Path

# When integration tests are pointed at a separate test DB via
# ``AGG_TEST_DATABASE_URL``, mirror that URL into ``AGG_DATABASE_URL`` *before*
# any ``aggrigator.*`` module gets imported. The worker layer uses
# ``aggrigator.db.async_session_factory`` directly (not via FastAPI deps), so
# we can't override it later — the engine has to be bound to the test DB
# from import time. Without this, the test session would write to the test
# DB while the worker reads from the dev DB and finds zero rows.
_test_db = os.environ.get("AGG_TEST_DATABASE_URL")
if _test_db:
    os.environ["AGG_DATABASE_URL"] = _test_db
    sync = _test_db.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    os.environ["AGG_DATABASE_URL_SYNC"] = sync

import pytest  # noqa: E402  — must come after env mutation

ODDSAPI_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "oddsapi"


@pytest.fixture(scope="session")
def oddsapi_fixture_dir() -> Path:
    if not ODDSAPI_FIXTURE_DIR.exists() or not any(ODDSAPI_FIXTURE_DIR.iterdir()):
        pytest.skip(
            f"odds-api fixtures not found at {ODDSAPI_FIXTURE_DIR} — "
            "run `python -m aggrigator.scripts.capture_oddsapi_fixtures`"
        )
    return ODDSAPI_FIXTURE_DIR


@pytest.fixture(scope="session")
def fixture_odds_client(oddsapi_fixture_dir: Path):
    from tests.fixtures.oddsapi_client import FixtureOddsClient
    return FixtureOddsClient(oddsapi_fixture_dir)


@pytest.fixture(scope="session")
def all_event_payloads(fixture_odds_client) -> list[dict]:
    """Every internal-shape event payload across every captured league.

    Goes through ``to_internal_event_payload`` inside the fixture client so
    the translator is exercised end-to-end on each test run.
    """
    from aggrigator.ingest.odds_api_translate import INTERNAL_TO_ODDSAPI_LEAGUE

    out: list[dict] = []
    for league_id in INTERNAL_TO_ODDSAPI_LEAGUE:
        out.extend(fixture_odds_client.get_events(league_id=league_id))
    return out
