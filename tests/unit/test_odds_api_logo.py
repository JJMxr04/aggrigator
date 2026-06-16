"""fetch_participant_logo returns (bytes, content_type) or None on 404."""

from __future__ import annotations

import httpx

from aggrigator.ingest.odds_api_http import OddsApiHttpClient


def _client_with_handler(handler) -> OddsApiHttpClient:
    client = OddsApiHttpClient(
        base_url="https://api.odds-api.io/v3", api_key="TESTKEY", max_retries=0
    )
    client.client.close()
    client.client = httpx.Client(
        base_url="https://api.odds-api.io/v3",
        transport=httpx.MockTransport(handler),
    )
    return client


def test_fetch_logo_returns_bytes_and_type():
    def handler(request):
        assert request.url.path == "/v3/participants/38/logo"
        assert request.url.params.get("apiKey") == "TESTKEY"
        return httpx.Response(
            200, content=b"\x89PNG\r\n", headers={"content-type": "image/png"}
        )

    client = _client_with_handler(handler)
    result = client.fetch_participant_logo("38")
    assert result == (b"\x89PNG\r\n", "image/png")


def test_fetch_logo_404_returns_none():
    def handler(request):
        return httpx.Response(404, json={"error": "no logo"})

    client = _client_with_handler(handler)
    assert client.fetch_participant_logo("999") is None
