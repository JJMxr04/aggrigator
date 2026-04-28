"""Fixture-backed SGO client.

Reads the same captured JSON that ``sports-scores/simulator/sportsgame/`` reads.
Lets the ingest pipeline run end-to-end with zero network or DB. The fixture
filenames match what MDProject's ``SportsGameOddsClient`` expects in fixture
mode — that's the load-bearing detail that lets us run parity tests against
both implementations on the same input.

Filenames (under ``fixture_dir``):

- ``sports.json``                       — list of sports
- ``leagues__sport-{ID}.json``          — leagues for a sport
- ``leagues__all.json``                 — all leagues
- ``events__league-{ID}.json``          — events for a league
- ``events__all.json``                  — all events
- ``teams__league-{ID}.json``           — teams for a league
- ``usage.json``                        — account usage
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

from aggrigator.ingest.client import SgoError

logger = logging.getLogger(__name__)


class FixtureSgoClient:
    """Loads captured payloads and yields them like the real SGO client would."""

    def __init__(self, fixture_dir: str | Path) -> None:
        self.fixture_dir = Path(fixture_dir)
        if not self.fixture_dir.exists():
            raise SgoError(f"Fixture directory does not exist: {self.fixture_dir}")

    # ---- public surface -----------------------------------------------------

    def get_account_usage(self) -> dict:
        body = self._read("usage.json")
        return (body or {}).get("data", body) or {}

    def get_sports(self) -> list[dict]:
        body = self._read("sports.json")
        return (body or {}).get("data", []) or []

    def get_leagues(self, sport_id: str | None = None) -> list[dict]:
        if sport_id:
            body = self._read(f"leagues__sport-{sport_id}.json")
        else:
            body = self._read("leagues__all.json")
        return (body or {}).get("data", []) or []

    def get_teams(self, league_id: str) -> list[dict]:
        body = self._read(f"teams__league-{league_id}.json")
        return (body or {}).get("data", []) or []

    def get_events(
        self,
        *,
        league_id: str | None = None,
        event_id: str | None = None,
        starts_after: str | None = None,
        starts_before: str | None = None,
        live: bool | None = None,
        finalized: bool | None = None,
        odds_available: bool | None = True,
        odd_ids: list[str] | None = None,
        include_open_close: bool | None = None,
        include_opposing_odds: bool | None = None,
        include_alt_lines: bool | None = None,
        bookmaker_id: str | None = None,
        limit: int = 50,
        max_pages: int | None = None,
    ) -> Iterator[dict]:
        if event_id:
            for ev in self._iter_all_events():
                if ev.get("eventID") == event_id:
                    yield ev
                    return
            return

        if league_id:
            body = self._read(f"events__league-{league_id}.json")
            events = (body or {}).get("data", []) or []
        else:
            events = list(self._iter_all_events())

        for ev in events:
            if not _matches_window(ev, starts_after, starts_before):
                continue
            if not _matches_status(ev, live=live, finalized=finalized):
                continue
            yield ev

    def get_event(self, event_id: str, *, include_open_close: bool = True) -> dict | None:
        events = list(self.get_events(event_id=event_id, max_pages=1, odds_available=None))
        return events[0] if events else None

    # ---- internals ----------------------------------------------------------

    def _read(self, name: str) -> dict | None:
        path = self.fixture_dir / name
        if not path.exists():
            logger.debug("Fixture %s missing", path)
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise SgoError(f"Invalid JSON in fixture {path}: {exc}") from exc

    def _iter_all_events(self) -> Iterator[dict]:
        for path in sorted(self.fixture_dir.glob("events__league-*.json")):
            body = self._read(path.name)
            for ev in (body or {}).get("data", []) or []:
                yield ev


def _status_field(event: dict, field: str):
    status = event.get("status") or {}
    if field in status:
        return status[field]
    return event.get(field)


def _matches_window(ev: dict, starts_after: str | None, starts_before: str | None) -> bool:
    ts = _status_field(ev, "startsAt")
    if starts_after and ts and ts < starts_after:
        return False
    if starts_before and ts and ts > starts_before:
        return False
    return True


def _matches_status(ev: dict, *, live: bool | None, finalized: bool | None) -> bool:
    if live is not None and bool(_status_field(ev, "live")) != live:
        return False
    if finalized is not None and bool(_status_field(ev, "finalized")) != finalized:
        return False
    return True
