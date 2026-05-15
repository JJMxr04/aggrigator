"""Capture per-league odds-api.io payloads for the test suite.

Writes raw upstream JSON to ``tests/fixtures/oddsapi/`` so ``FixtureOddsClient``
(the test double for ``OddsApiHttpClient``) can replay them offline. Run once
locally with an active key::

    ODDSAPI_API_KEY=... python -m aggrigator.scripts.capture_oddsapi_fixtures

Captures one file per mapped league in ``INTERNAL_TO_ODDSAPI_LEAGUE``:

    sports.json
    leagues__<sport_slug>.json
    events__<league_id>.json
    odds_multi__<league_id>.json

The fixture client re-translates these into internal-shape payloads on every
test run, so the translator path is exercised end-to-end.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx

from aggrigator.ingest.odds_api_translate import (
    INTERNAL_TO_ODDSAPI_LEAGUE,
    INTERNAL_TO_ODDSAPI_SPORT,
)

logger = logging.getLogger(__name__)

FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "oddsapi"
)

_APIKEY_RE = re.compile(r"(apiKey=)[^&\s]+")


def _redact(url: str) -> str:
    return _APIKEY_RE.sub(r"\1REDACTED", url)


def _get(client: httpx.Client, path: str, params: dict[str, Any]) -> Any:
    resp = client.get(path, params=params)
    print(f"  GET {_redact(str(resp.request.url))} → {resp.status_code}")
    resp.raise_for_status()
    return resp.json()


def _save(name: str, payload: Any) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DIR / name).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False)
    )
    print(f"  → wrote {name}")


def _league_to_sport(league_id: str) -> str:
    return {
        "MLB": "BASEBALL", "MLS": "SOCCER", "NBA": "BASKETBALL",
        "NFL": "FOOTBALL", "NHL": "HOCKEY",
        "UEFA_CHAMPIONS_LEAGUE": "SOCCER", "EPL": "SOCCER",
    }[league_id]


def capture(api_key: str) -> int:
    base_url = "https://api.odds-api.io/v3"
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        params = {"apiKey": api_key}

        print("[sports]")
        sports = _get(client, "/sports", params)
        _save("sports.json", sports)

        seen_sport_slugs: set[str] = set()
        for league_id, league_slug in INTERNAL_TO_ODDSAPI_LEAGUE.items():
            sport_id = _league_to_sport(league_id)
            sport_slug = INTERNAL_TO_ODDSAPI_SPORT.get(sport_id)
            if sport_slug is None:
                print(f"  skip {league_id}: no sport_slug for {sport_id}")
                continue

            if sport_slug not in seen_sport_slugs:
                print(f"\n[leagues] sport={sport_slug}")
                leagues = _get(client, "/leagues", {**params, "sport": sport_slug})
                _save(f"leagues__{sport_slug}.json", leagues)
                seen_sport_slugs.add(sport_slug)

            print(f"\n[events] {league_id} ({sport_slug}/{league_slug})")
            events = _get(
                client, "/events",
                {**params, "sport": sport_slug, "league": league_slug},
            )
            _save(f"events__{league_id}.json", events)

            pending_ids = [
                str(e["id"])
                for e in (events if isinstance(events, list) else [])
                if isinstance(e, dict) and e.get("status") == "pending" and e.get("id")
            ]
            if pending_ids:
                # /odds/multi caps at 10 events per call. The fixture client
                # only needs one representative batch so cap the capture.
                batch = pending_ids[:10]
                print(f"  /odds/multi for {len(batch)} pending event(s)")
                multi = _get(
                    client, "/odds/multi",
                    {
                        **params,
                        "eventIds": ",".join(batch),
                        "bookmakers": "DraftKings,FanDuel",
                    },
                )
                _save(f"odds_multi__{league_id}.json", multi)
            else:
                _save(f"odds_multi__{league_id}.json", [])

    print(f"\nCapture complete. Fixtures in {FIXTURE_DIR}")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-key", default=os.environ.get("ODDSAPI_API_KEY", ""),
    )
    args = parser.parse_args()
    if not args.api_key:
        print("ODDSAPI_API_KEY not set", file=sys.stderr)
        return 2
    return capture(args.api_key)


if __name__ == "__main__":
    raise SystemExit(main())
