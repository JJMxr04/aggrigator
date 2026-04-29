"""Shared fixtures.

Phase 0 has no Postgres in the loop — every test runs against captured SGO JSON
straight off disk. Postgres / Redis / API integration tests land in Phase 1.
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

# The captured SGO payloads from the simulator project. We reuse them — there's
# no point keeping a second copy.
# The captured SGO payloads live in the sports-scores-sim project.
# We keep two candidate paths here so renames in the simulator repo don't
# silently turn the parity test into a skip.
_PBL = Path(__file__).resolve().parent.parent.parent
_FIXTURE_CANDIDATES = [
    _PBL / "sports-scores-sim" / "json" / "sports_game" / "simulator",
    _PBL / "sports-scores" / "json" / "sports_game" / "simulator",
]
SGO_FIXTURE_DIR = next((p for p in _FIXTURE_CANDIDATES if p.exists()), _FIXTURE_CANDIDATES[0])


@pytest.fixture(scope="session")
def sgo_fixture_dir() -> Path:
    if not SGO_FIXTURE_DIR.exists():
        pytest.skip(f"SGO fixtures not found at {SGO_FIXTURE_DIR}")
    return SGO_FIXTURE_DIR


@pytest.fixture(scope="session")
def all_event_payloads(sgo_fixture_dir: Path) -> list[dict]:
    """Every event payload from every captured league file."""
    import json

    out: list[dict] = []
    for path in sorted(sgo_fixture_dir.glob("events__league-*.json")):
        body = json.loads(path.read_text())
        out.extend(body.get("data", []))
    return out
