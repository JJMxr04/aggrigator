"""Async HTTP client for TheSportsDB v1 free tier.

Async port of ``sports-scores-sim/api/thesportsdb.py``. The aggrigator
uses ``httpx.AsyncClient`` to match its other ingest paths.

TheSportsDB v1 puts the API key in the URL path, not a header or query
string. The public free key is ``"3"``. The aggrigator default reads
from ``THESPORTSDB_API_KEY`` (added to ``aggrigator.config`` by Phase 3);
operators can override per-instance if a paid key becomes necessary
(v2 / Patreon endpoints).

Endpoints wrapped here:
  - ``GET /eventsround.php?id=<leagueId>&r=<round>&s=<season>`` — full
    matchweek for one league. Returns ``{"events": [...]}`` or
    ``{"events": null}``. Verified working on the free key for EPL +
    MLS (see ``plans/analytics/TheSportsDB/00-overview.md``).
  - ``GET /eventsseason.php?id=<leagueId>&s=<season>`` — kept for
    parity / ops tooling. WARNING: silently truncates to ~15 events
    on the free key; use ``events_round`` for backfill.
  - ``GET /all_leagues.php`` — used by ops tooling to verify
    ``League.thesportsdb_id`` values during registry maintenance.

Rate limit on the free key lands around 30 req/min. Client throttles
to ``min_interval=1.5s`` by default and exponentially backs off on
429 up to ``max_backoff`` seconds, retrying indefinitely (the free
tier always recovers — no point bailing the whole orchestrator).

No usage / quota header parsing — free tier doesn't surface them.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)


class TheSportsDbError(Exception):
    """Non-recoverable failure (network error after retries, or
    malformed JSON). 429 is handled internally and not surfaced."""


class TheSportsDbClient:
    BASE = "https://www.thesportsdb.com/api/v1/json/{key}"

    def __init__(
        self,
        api_key: str = "3",
        http: httpx.AsyncClient | None = None,
        min_interval: float = 1.5,
        max_backoff: float = 60.0,
    ):
        self._api_key = api_key
        self._base = self.BASE.format(key=api_key)
        self._http = http or httpx.AsyncClient(timeout=30.0)
        self._owns_http = http is None
        self._min_interval = min_interval
        self._max_backoff = max_backoff
        self._last_request_at = 0.0

    async def __aenter__(self) -> "TheSportsDbClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        delay = self._min_interval - (time.monotonic() - self._last_request_at)
        if delay > 0:
            await asyncio.sleep(delay)

    async def _request(self, path: str, params: dict | None = None) -> dict:
        url = f"{self._base}/{path.lstrip('/')}"
        attempt = 0
        while True:
            await self._throttle()
            response = await self._http.get(url, params=params or {})
            self._last_request_at = time.monotonic()

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    wait = float(retry_after)
                else:
                    wait = min(self._max_backoff, 2.0 ** attempt)
                attempt += 1
                logger.warning(
                    "TheSportsDB 429 on %s; sleeping %.1fs (attempt %d)",
                    path, wait, attempt,
                )
                await asyncio.sleep(wait)
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise TheSportsDbError(f"{path}: {e}") from e

            try:
                return response.json()
            except ValueError as e:
                raise TheSportsDbError(f"{path}: malformed JSON: {e}") from e

    async def events_round(
        self, league_id: str, round_num: int, season: str,
    ) -> list[dict]:
        """All events for one round of one season.

        Returns the ``events`` list (empty if the season + round combo
        has no data, which is how the orchestrator detects the end of
        an MLS-style irregular season).
        """
        data = await self._request(
            "eventsround.php",
            {"id": league_id, "r": round_num, "s": season},
        )
        return data.get("events") or []

    async def events_season(
        self, league_id: str, season: str,
    ) -> list[dict]:
        """All events in one season.

        WARNING: free key truncates to ~15 events. Do not use for
        backfill — use ``events_round`` instead. Kept for ops parity
        + the Phase 1 MLS round-structure verification probe.
        """
        data = await self._request(
            "eventsseason.php",
            {"id": league_id, "s": season},
        )
        return data.get("events") or []

    async def all_leagues(self) -> list[dict]:
        """Full league directory. Used by ops tooling to verify
        ``League.thesportsdb_id`` mappings."""
        data = await self._request("all_leagues.php")
        return data.get("leagues") or []
