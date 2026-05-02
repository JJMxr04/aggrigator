"""Async HTTP SGO client.

Talks to either the real ``api.sportsgameodds.com/v2`` or the local simulator
at ``http://127.0.0.1:8765/v2`` — both speak the same v2 surface so it's a
single class with a configurable base URL. Implements the same Protocol as
``FixtureSgoClient`` so the orchestrator works against any of them.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import httpx

from aggrigator.ingest.client import QuotaExceeded, SgoError

logger = logging.getLogger(__name__)


class SgoHttpClient:
    """Sync interface to match the existing ``SgoClient`` Protocol.

    We use a sync ``httpx.Client`` here because the orchestrator's loop is
    cooperative and the SGO calls are bound by the upstream rate limit
    anyway (10/min token bucket). When the orchestrator is invoked from an
    ARQ task, the surrounding async event loop just awaits the worker job;
    inside the job we run sync HTTP calls and async DB writes. The async
    variant can land later if needed.

    Rate-limit behavior:

    - **Pre-emptive throttle**: ``min_interval`` seconds between requests
      via instance state. Default 7s = ~8/min, safely under SGO's 10/min
      free-tier cap. Stops short bursts (e.g. seed walking 12 sports
      back-to-back) from tripping 429 in the first place.
    - **429 retry**: on a quota response, sleep + retry up to ``max_retries``
      times. Honors ``Retry-After`` header if present (seconds form);
      otherwise exponential backoff (1s, 2s, 4s …) capped at 60s.
    - Both are sleep-based blocks. That's fine here because the surrounding
      orchestrator runs SGO calls sequentially anyway — see the class
      docstring above. If we ever go async, switch to ``asyncio.sleep``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        timeout: float = 30.0,
        min_interval: float = 0.0,
        max_retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"x-api-key": api_key} if api_key else {}
        self.client = httpx.Client(
            base_url=self.base_url, headers=headers, timeout=timeout,
        )
        self.min_interval = max(0.0, min_interval)
        self.max_retries = max(0, max_retries)
        self._last_request_at: float = 0.0

    def close(self) -> None:
        self.client.close()

    # ---- public surface (matches FixtureSgoClient) -------------------------

    def get_account_usage(self) -> dict:
        body = self._request("account/usage")
        return body.get("data") or {}

    def get_sports(self) -> list[dict]:
        body = self._request("sports")
        return body.get("data") or []

    def get_leagues(self, sport_id: str | None = None) -> list[dict]:
        body = self._request("leagues", {"sportID": sport_id})
        return body.get("data") or []

    def get_teams(self, league_id: str) -> list[dict]:
        body = self._request("teams", {"leagueID": league_id, "limit": 100})
        return body.get("data") or []

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
        params: dict[str, Any] = {
            "leagueID": league_id,
            "eventID": event_id,
            "startsAfter": starts_after,
            "startsBefore": starts_before,
            "live": _bool(live),
            "finalized": _bool(finalized),
            "oddsAvailable": _bool(odds_available),
            "oddID": ",".join(odd_ids) if odd_ids else None,
            "includeOpenCloseOdds": _bool(include_open_close),
            "includeOpposingOdds": _bool(include_opposing_odds),
            "includeAltLines": _bool(include_alt_lines),
            "bookmakerID": bookmaker_id,
            "limit": limit,
        }
        cursor: str | None = None
        pages = 0
        while True:
            if cursor:
                params["cursor"] = cursor
            body = self._request("events", params)
            for ev in body.get("data") or []:
                yield ev
            cursor = body.get("nextCursor")
            pages += 1
            if not cursor:
                return
            if max_pages is not None and pages >= max_pages:
                return

    def get_event(
        self, event_id: str, *, include_open_close: bool = True
    ) -> dict | None:
        events = list(self.get_events(
            event_id=event_id,
            include_open_close=include_open_close,
            include_opposing_odds=True,
            odds_available=None,
            max_pages=1,
        ))
        return events[0] if events else None

    # ---- transport ---------------------------------------------------------

    _MAX_RETRY_WAIT = 60.0  # cap any single sleep so a runaway header
                            #   (e.g. Retry-After: 86400) can't freeze the cron.

    def _throttle(self) -> None:
        """Sleep just enough to maintain ``self.min_interval`` since the last
        request. No-op when min_interval is 0 (default for fixtures / tests)."""
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        """Parse the Retry-After header (seconds form only — HTTP-date form
        is uncommon in API rate-limit responses and not worth handling)."""
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return None

    def _request(self, path: str, params: dict[str, Any] | None = None) -> dict:
        clean = {k: v for k, v in (params or {}).items() if v is not None}

        backoff = 1.0
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.client.get(f"/{path.lstrip('/')}", params=clean)
            except httpx.HTTPError as exc:
                raise SgoError(f"GET {path} failed: {exc}") from exc
            finally:
                self._last_request_at = time.monotonic()

            if resp.status_code == 429:
                if attempt >= self.max_retries:
                    logger.warning(
                        "SGO 429 on %s after %d retries — giving up",
                        path, attempt,
                    )
                    raise QuotaExceeded(
                        f"SGO 429 on {path} (after {attempt} retries)"
                    )
                wait = self._parse_retry_after(resp.headers.get("Retry-After"))
                if wait is None:
                    wait = backoff
                wait = min(wait, self._MAX_RETRY_WAIT)
                logger.warning(
                    "SGO 429 on %s — sleeping %.1fs (retry %d/%d)",
                    path, wait, attempt + 1, self.max_retries,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, self._MAX_RETRY_WAIT)
                continue

            if resp.status_code >= 400:
                raise SgoError(
                    f"GET {path} returned {resp.status_code}: {resp.text[:200]}"
                )
            body = resp.json()
            if body.get("success") is False:
                raise SgoError(body.get("error", "Unknown SGO error"))
            return body

        # Loop exits via ``return`` or ``raise`` — this is unreachable but
        # keeps the type-checker happy without forcing an Optional return.
        raise SgoError(f"unreachable: retry loop fell through for {path}")


def _bool(value: bool | None) -> str | None:
    if value is None:
        return None
    return "true" if value else "false"
