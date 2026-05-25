"""``FixtureOddsClient`` — offline replacement for ``OddsApiHttpClient``.

Reads raw upstream JSON captured by
``aggrigator.scripts.capture_oddsapi_fixtures`` from ``tests/fixtures/oddsapi/``
and replays it through the same ``to_internal_event_payload`` translator the
live HTTP client uses, so consumers see byte-identical internal-shape
payloads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from aggrigator.ingest.client import OddsClient, OddsClientError
from aggrigator.ingest.odds_api_translate import (
    INTERNAL_TO_ODDSAPI_LEAGUE,
    INTERNAL_TO_ODDSAPI_SPORT,
    ODDSAPI_TO_INTERNAL_LEAGUE,
    ODDSAPI_TO_INTERNAL_SPORT,
    to_internal_event_payload,
)


class FixtureOddsClient:
    """Implements ``OddsClient`` against captured JSON."""

    def __init__(self, fixture_dir: str | Path) -> None:
        self.fixture_dir = Path(fixture_dir)
        if not self.fixture_dir.exists():
            raise OddsClientError(
                f"fixture dir {self.fixture_dir} does not exist — "
                "run `python -m aggrigator.scripts.capture_oddsapi_fixtures`"
            )

    def _read(self, name: str) -> object:
        path = self.fixture_dir / name
        if not path.exists():
            raise OddsClientError(f"missing fixture file: {path}")
        return json.loads(path.read_text())

    def get_account_usage(self) -> dict:
        return {
            "tier": "fixture",
            "isActive": True,
            "rateLimits": {
                "per-hour": {"max-requests": "unlimited", "current-requests": 0},
                "per-month": {"max-entities": "unlimited", "current-entities": 0},
            },
        }

    def get_sports(self) -> list[dict]:
        body = self._read("sports.json")
        items = body if isinstance(body, list) else []
        out: list[dict] = []
        for s in items:
            if not isinstance(s, dict):
                continue
            slug = s.get("slug") or ""
            internal_id = (
                ODDSAPI_TO_INTERNAL_SPORT.get(slug)
                or slug.upper().replace("-", "_")
            )
            out.append({
                "sportID": internal_id,
                "name": s.get("name") or internal_id.title(),
                "shortName": s.get("name") or internal_id.title(),
            })
        return out

    def get_leagues(self, sport_id: str | None = None) -> list[dict]:
        if sport_id is None:
            sport_slugs = list(INTERNAL_TO_ODDSAPI_SPORT.values())
        else:
            slug = INTERNAL_TO_ODDSAPI_SPORT.get(sport_id)
            if slug is None:
                return []
            sport_slugs = [slug]

        out: list[dict] = []
        for slug in sport_slugs:
            path = self.fixture_dir / f"leagues__{slug}.json"
            if not path.exists():
                continue
            body = json.loads(path.read_text())
            leagues = body if isinstance(body, list) else []
            internal_sport = ODDSAPI_TO_INTERNAL_SPORT.get(slug)
            if internal_sport is None:
                continue
            for ln in leagues:
                if not isinstance(ln, dict):
                    continue
                lslug = ln.get("slug") or ""
                internal_league = ODDSAPI_TO_INTERNAL_LEAGUE.get(lslug)
                if internal_league is None:
                    continue
                out.append({
                    "leagueID": internal_league[:48],
                    "sportID": internal_sport[:32],
                    "name": ln.get("name") or internal_league,
                    "shortName": (ln.get("name") or internal_league)[:48],
                })
        return out

    def get_league_activity(self) -> dict[str, int]:
        """Empty dict = "no activity info" — orchestrator falls back to
        walking every active league. Fixtures don't carry ``eventsCount``,
        and the existing per-league ``events__<id>.json`` fixtures are
        enough to drive ingest tests without it."""
        return {}

    def get_teams(self, league_id: str) -> list[dict]:
        return []

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
        if event_id is not None:
            payload = self.get_event(event_id)
            if payload is not None:
                yield payload
            return
        if league_id is None:
            return

        events_path = self.fixture_dir / f"events__{league_id}.json"
        if not events_path.exists():
            return
        events_body = json.loads(events_path.read_text())
        events = events_body if isinstance(events_body, list) else []

        multi_path = self.fixture_dir / f"odds_multi__{league_id}.json"
        odds_by_id: dict[str, dict] = {}
        if multi_path.exists():
            multi_body = json.loads(multi_path.read_text())
            if isinstance(multi_body, list):
                for o in multi_body:
                    if isinstance(o, dict) and o.get("id") is not None:
                        odds_by_id[str(o["id"])] = o

        for ev in events:
            if not isinstance(ev, dict):
                continue
            status = ev.get("status")
            if status == "live":
                continue
            odds = odds_by_id.get(str(ev.get("id"))) if status == "pending" else None
            try:
                payload = to_internal_event_payload(ev, odds)
            except Exception:  # noqa: BLE001 — fixtures replay translator
                continue
            if payload is not None:
                yield payload

    def get_event(
        self, event_id: str, *, include_open_close: bool = True,
    ) -> dict | None:
        # Walk every events__*.json for the requested event id.
        for path in sorted(self.fixture_dir.glob("events__*.json")):
            league_id = path.stem.removeprefix("events__")
            body = json.loads(path.read_text())
            events = body if isinstance(body, list) else []
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                if str(ev.get("id")) != str(event_id):
                    continue
                if ev.get("status") == "live":
                    return None
                odds = None
                if ev.get("status") == "pending":
                    multi_path = (
                        self.fixture_dir / f"odds_multi__{league_id}.json"
                    )
                    if multi_path.exists():
                        for o in json.loads(multi_path.read_text()):
                            if isinstance(o, dict) and str(o.get("id")) == str(event_id):
                                odds = o
                                break
                try:
                    return to_internal_event_payload(ev, odds)
                except Exception:  # noqa: BLE001
                    return None
        return None


# Stable accessor — the OddsClient Protocol doesn't require it, but tests can
# use this to assert the client is the fixture one.
def is_fixture_client(client: OddsClient) -> bool:
    return isinstance(client, FixtureOddsClient)
