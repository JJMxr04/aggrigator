"""Unit tests for ``OddsApiHttpClient`` using ``httpx.MockTransport``.

No respx dependency — vanilla httpx supports response injection via a
custom transport, which is enough for the path coverage we need.

Coverage focus (best-practices.md §3, §6; cancelled-suspended.md §4):

- ``/odds/multi`` batching: pending events flushed in groups of 10; the
  single-event ``/odds`` endpoint is NEVER called for >1 pending event.
- Live events dropped at the adapter (no /odds call).
- Settled events translate WITHOUT calling /odds.
- 404 → ``NotFoundError`` raised cleanly (not a crash).
- 401 → ``InvalidApiKeyError``.
- URL redactor strips ``apiKey=<value>`` from log messages.
- Rate-limit headers stashed on the client for the quota pacer.
"""

from __future__ import annotations

import httpx
import pytest

from aggrigator.ingest.odds_api_errors import (
    InvalidApiKeyError,
    NotFoundError,
)
from aggrigator.ingest.odds_api_http import OddsApiHttpClient, redact_api_key


# ---- helpers ---------------------------------------------------------------


def _client_with_handler(handler) -> OddsApiHttpClient:
    """Build a client whose internal httpx.Client uses our mock transport."""
    client = OddsApiHttpClient(
        base_url="https://api.odds-api.io/v3",
        api_key="TESTKEY",
        max_retries=0,
    )
    # Swap the underlying httpx client for one with our mock transport.
    client.client.close()
    client.client = httpx.Client(
        base_url="https://api.odds-api.io/v3",
        transport=httpx.MockTransport(handler),
    )
    return client


def _ok_json(payload, *, headers=None):
    h = {
        "x-ratelimit-limit": "100",
        "x-ratelimit-remaining": "95",
        "x-ratelimit-reset": "2026-05-10T18:00:00Z",
    }
    if headers:
        h.update(headers)
    return httpx.Response(200, json=payload, headers=h)


# ---- redactor --------------------------------------------------------------


def test_redact_api_key_strips_apikey_param() -> None:
    url = "https://api.odds-api.io/v3/sports?apiKey=supersecretvalue&other=1"
    assert "supersecretvalue" not in redact_api_key(url)
    assert "apiKey=REDACTED" in redact_api_key(url)


def test_redact_api_key_handles_no_apikey() -> None:
    url = "https://api.odds-api.io/v3/sports"
    assert redact_api_key(url) == url


# ---- header capture --------------------------------------------------------


def test_rate_limit_headers_stashed_on_client() -> None:
    def handler(request):
        return _ok_json([])

    client = _client_with_handler(handler)
    client.get_sports()
    assert client.last_ratelimit_limit == 100
    assert client.last_ratelimit_remaining == 95
    assert client.last_ratelimit_reset == "2026-05-10T18:00:00Z"


# ---- error mapping ---------------------------------------------------------


def test_404_raises_not_found_error() -> None:
    def handler(request):
        return httpx.Response(404, json={"error": "missing"})

    client = _client_with_handler(handler)
    with pytest.raises(NotFoundError):
        client._send("/events/999", {})


def test_401_raises_invalid_api_key_error() -> None:
    def handler(request):
        return httpx.Response(401, json={"error": "bad key"})

    client = _client_with_handler(handler)
    with pytest.raises(InvalidApiKeyError):
        client._send("/sports", {})


def test_get_single_event_returns_none_on_404() -> None:
    def handler(request):
        return httpx.Response(404, json={"error": "missing"})

    client = _client_with_handler(handler)
    assert client.get_event("999") is None


# ---- batching --------------------------------------------------------------


def test_get_events_uses_odds_multi_not_single_odds() -> None:
    """Cycle with 12 pending events should produce 2 calls to /odds/multi
    and ZERO calls to /odds (single-event)."""
    multi_calls: list[str] = []
    single_odds_calls: list[str] = []

    events_payload = [
        {
            "id": i,
            "home": "H", "away": "A",
            "homeId": 1, "awayId": 2,
            "date": "2026-05-10T19:00:00Z",
            "status": "pending",
            "sport": {"name": "Baseball", "slug": "baseball"},
            "league": {"name": "USA - MLB", "slug": "usa-mlb"},
            "scores": {},
        }
        for i in range(1, 13)
    ]

    def handler(request):
        path = request.url.path
        if path == "/v3/events":
            return _ok_json(events_payload)
        if path == "/v3/odds/multi":
            multi_calls.append(str(request.url))
            ids = request.url.params.get("eventIds", "").split(",")
            return _ok_json([
                {
                    "id": int(i),
                    "home": "H", "away": "A",
                    "date": "2026-05-10T19:00:00Z", "status": "pending",
                    "sport": {"slug": "baseball"},
                    "league": {"slug": "usa-mlb"},
                    "urls": {},
                    "bookmakers": {
                        "Bet365": [{
                            "name": "ML",
                            "updatedAt": "2026-05-10T17:00:00Z",
                            "odds": [{"home": "2.00", "away": "1.90"}],
                        }],
                    },
                }
                for i in ids if i
            ])
        if path == "/v3/odds":
            single_odds_calls.append(str(request.url))
            return _ok_json({})
        return httpx.Response(404)

    client = _client_with_handler(handler)
    out = list(client.get_events(league_id="MLB"))
    assert len(out) == 12
    assert len(single_odds_calls) == 0
    # 12 events / 10 per batch = 2 calls
    assert len(multi_calls) == 2


def test_live_events_dropped_without_fetching_odds() -> None:
    multi_called = False
    events_payload = [
        {
            "id": 1, "home": "H", "away": "A", "homeId": 1, "awayId": 2,
            "date": "2026-05-10T19:00:00Z", "status": "live",
            "sport": {"slug": "baseball"}, "league": {"slug": "usa-mlb"},
            "scores": {"home": 1, "away": 0},
        }
    ]

    def handler(request):
        nonlocal multi_called
        if request.url.path == "/v3/events":
            return _ok_json(events_payload)
        if request.url.path in ("/v3/odds/multi", "/v3/odds"):
            multi_called = True
        return _ok_json({})

    client = _client_with_handler(handler)
    out = list(client.get_events(league_id="MLB"))
    assert out == []
    assert multi_called is False


def test_settled_events_translate_without_odds_call() -> None:
    odds_called = False
    events_payload = [
        {
            "id": 1, "home": "H", "away": "A", "homeId": 1, "awayId": 2,
            "date": "2026-05-09T19:00:00Z", "status": "settled",
            "sport": {"slug": "baseball"}, "league": {"slug": "usa-mlb"},
            "scores": {"home": 5, "away": 3},
        }
    ]

    def handler(request):
        nonlocal odds_called
        if request.url.path == "/v3/events":
            return _ok_json(events_payload)
        if request.url.path in ("/v3/odds/multi", "/v3/odds"):
            odds_called = True
            return _ok_json({})
        return httpx.Response(404)

    client = _client_with_handler(handler)
    out = list(client.get_events(league_id="MLB"))
    assert len(out) == 1
    assert out[0]["status"]["finalized"] is True
    assert odds_called is False


# ---- unknown league guard --------------------------------------------------


def test_unknown_league_yields_no_events_and_no_http_calls() -> None:
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.path)
        return _ok_json([])

    client = _client_with_handler(handler)
    out = list(client.get_events(league_id="UNKNOWN_LEAGUE"))
    assert out == []
    assert calls == []  # no upstream calls when we can't map the slug


# ---- catalog-driven _league_to_sport ----------------------------------------


def test_league_to_sport_reads_catalog(monkeypatch) -> None:
    """_league_to_sport must read LEAGUE_TO_SPORT from the catalog module so
    that entries added by the generator (e.g. JAPAN_NPB) are visible without
    a code change."""
    from aggrigator.ingest import odds_api_catalog as cat

    monkeypatch.setitem(cat.LEAGUE_TO_SPORT, "JAPAN_NPB", "BASEBALL")
    client = OddsApiHttpClient(
        base_url="https://api.odds-api.io/v3",
        api_key="x",
        max_retries=0,
    )
    assert client._league_to_sport("JAPAN_NPB") == "BASEBALL"
    assert client._league_to_sport("MLB") == "BASEBALL"
    assert client._league_to_sport("nope") is None
