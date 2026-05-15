"""Phase 0 probe — capture odds-api.io payloads + resolve open questions.

Run locally with an active API key. The probe answers the empirical
questions the docs leave open
(see aggrigator-plan/odds-api/odds-api.md §8 Phase 0 +
python-sdk.md §5):

1. Which base URL is live — ``api.odds-api.io`` or ``api2.odds-api.io``?
2. What sport slugs exist? (Update ``INTERNAL_TO_ODDSAPI_SPORT`` in
   ``odds_api_translate.py``.)
3. What league slugs exist per sport? (Update
   ``INTERNAL_TO_ODDSAPI_LEAGUE``.)
4. What does a real event payload look like? Save as fixture.
5. What does ``/odds`` return for a pending event? Save as fixture.
6. Does ``/dropping-odds`` return 403 on free tier? (Sanity check
   for tier inference.)

Usage:

    ODDSAPI_API_KEY=... python -m aggrigator.scripts.probe_oddsapi
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Where to write fixture JSON for the translator tests to consume.
FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "oddsapi"


def _redact_apikey(url: str) -> str:
    """Best-effort URL redactor so console output / log scrapers don't see
    the key even if probe output is pasted into a ticket."""
    import re
    return re.sub(r"(apiKey=)[^&\s]+", r"\1REDACTED", url)


def _get(client: httpx.Client, path: str, params: dict[str, Any]) -> tuple[int, Any]:
    resp = client.get(path, params=params)
    redacted = _redact_apikey(str(resp.request.url))
    print(f"  GET {redacted} → {resp.status_code} ({len(resp.content)} bytes)")
    print(f"    x-ratelimit-limit={resp.headers.get('x-ratelimit-limit')} "
          f"remaining={resp.headers.get('x-ratelimit-remaining')} "
          f"reset={resp.headers.get('x-ratelimit-reset')}")
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"_raw": resp.text[:500]}
    return resp.status_code, body


def _save(name: str, payload: Any) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    fp = FIXTURE_DIR / name
    fp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"  → saved {fp.relative_to(FIXTURE_DIR.parent.parent)}")


def probe_base_url(api_key: str) -> str:
    """Resolve the SDK vs docs base-URL discrepancy. Return the working host."""
    print("\n[base url] resolving api. vs api2. discrepancy")
    for base in ("https://api.odds-api.io/v3", "https://api2.odds-api.io/v3"):
        try:
            with httpx.Client(base_url=base, timeout=10.0) as c:
                r = c.get("/sports", params={"apiKey": api_key})
                print(f"  {base} → {r.status_code}")
                if r.status_code in (200, 401):
                    # 200 = working host; 401 = working host with bad key
                    return base
        except httpx.HTTPError as exc:
            print(f"  {base} → ERROR: {exc}")
    raise SystemExit("Neither api.odds-api.io nor api2.odds-api.io responded")


def probe_sports(client: httpx.Client, api_key: str) -> list[dict]:
    print("\n[sports] GET /sports")
    code, body = _get(client, "/sports", {"apiKey": api_key})
    if code == 200 and isinstance(body, list):
        _save("sports.json", body)
        print(f"  → {len(body)} sport(s):")
        for s in body[:10]:
            print(f"    - slug={s.get('slug')!r}  name={s.get('name')!r}")
        return body
    print(f"  unexpected response: {body}")
    return []


def probe_leagues(client: httpx.Client, api_key: str, sports: list[dict]) -> dict[str, list[dict]]:
    print("\n[leagues] GET /leagues?sport=... per sport")
    out: dict[str, list[dict]] = {}
    for s in sports:
        slug = s.get("slug")
        if not slug:
            continue
        code, body = _get(client, "/leagues", {"apiKey": api_key, "sport": slug})
        if code == 200 and isinstance(body, list):
            out[slug] = body
            print(f"  {slug}: {len(body)} league(s) — first 5:")
            for ln in body[:5]:
                print(f"    - slug={ln.get('slug')!r}  name={ln.get('name')!r}  events={ln.get('eventsCount')}")
        else:
            print(f"  {slug} → {code}: {body}")
    _save("leagues.json", out)
    return out


def probe_events_and_odds(client: httpx.Client, api_key: str, leagues: dict[str, list[dict]]) -> None:
    """Pick a league with events and capture one event + its odds."""
    print("\n[events + odds] capturing a fresh payload")
    # Find a league with events.
    candidate = None
    for sport_slug, lns in leagues.items():
        for ln in lns:
            if (ln.get("eventsCount") or 0) > 0:
                candidate = (sport_slug, ln.get("slug"))
                break
        if candidate:
            break
    if candidate is None:
        print("  no league with events available — skip")
        return

    sport_slug, league_slug = candidate
    print(f"  probing sport={sport_slug} league={league_slug}")
    code, events = _get(
        client, "/events",
        {"apiKey": api_key, "sport": sport_slug, "league": league_slug},
    )
    if code != 200 or not isinstance(events, list) or not events:
        print(f"  no events returned ({code})")
        return
    _save("events.json", events)
    print(f"  → {len(events)} event(s). First event:")
    first = events[0]
    print(f"    id={first.get('id')} status={first.get('status')} date={first.get('date')}")
    print(f"    home={first.get('home')!r} away={first.get('away')!r}")
    print(f"    scores={first.get('scores')}")

    event_id = first.get("id")
    if event_id is None:
        return

    # /odds for the first event
    code, odds = _get(
        client, "/odds",
        {"apiKey": api_key, "eventId": event_id, "bookmakers": "Bet365,Pinnacle"},
    )
    if code == 200:
        _save("odds.json", odds)
        if isinstance(odds, dict):
            books = list((odds.get("bookmakers") or {}).keys())
            print(f"  /odds: bookmakers returned = {books}")

    # /odds/multi for the first 3 events (or fewer)
    ids = ",".join(str(e.get("id")) for e in events[:3] if e.get("id") is not None)
    if ids:
        code, multi = _get(
            client, "/odds/multi",
            {"apiKey": api_key, "eventIds": ids, "bookmakers": "Bet365,Pinnacle"},
        )
        if code == 200:
            _save("odds_multi.json", multi)


def probe_paid_endpoint(client: httpx.Client, api_key: str) -> None:
    """Sanity check: paid-only endpoints should 403 on free tier."""
    print("\n[paid gate] expecting 403 on /dropping-odds (free tier)")
    code, body = _get(
        client, "/dropping-odds",
        {"apiKey": api_key, "sport": "football"},
    )
    print(f"  → {code} (403 expected; got something else?  body={body!r:.200})")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-key", default=os.environ.get("ODDSAPI_API_KEY", ""))
    args = parser.parse_args()
    if not args.api_key:
        print("ODDSAPI_API_KEY not set", file=sys.stderr)
        return 2

    base_url = probe_base_url(args.api_key)
    print(f"\n>>> using base_url={base_url}")
    print(f">>> writing fixtures to {FIXTURE_DIR}")

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        sports = probe_sports(client, args.api_key)
        leagues = probe_leagues(client, args.api_key, sports)
        probe_events_and_odds(client, args.api_key, leagues)
        probe_paid_endpoint(client, args.api_key)

    print("\nDone. Review fixtures + update slug maps in odds_api_translate.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
