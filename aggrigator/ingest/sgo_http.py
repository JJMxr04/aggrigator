"""Async HTTP SGO client.

Talks to either the real ``api.sportsgameodds.com/v2`` or the local simulator
at ``http://127.0.0.1:8765/v2`` — both speak the same v2 surface so it's a
single class with a configurable base URL. Implements the same Protocol as
``FixtureSgoClient`` so the orchestrator works against any of them.
"""

from __future__ import annotations

import logging
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
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"x-api-key": api_key} if api_key else {}
        self.client = httpx.Client(
            base_url=self.base_url, headers=headers, timeout=timeout,
        )

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

    def _request(self, path: str, params: dict[str, Any] | None = None) -> dict:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = self.client.get(f"/{path.lstrip('/')}", params=clean)
        except httpx.HTTPError as exc:
            raise SgoError(f"GET {path} failed: {exc}") from exc
        if resp.status_code == 429:
            # Caller should back off — token bucket / quota tracking lives in
            # the worker layer, not here.
            raise QuotaExceeded(f"SGO 429 on {path}")
        if resp.status_code >= 400:
            raise SgoError(f"GET {path} returned {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        if body.get("success") is False:
            raise SgoError(body.get("error", "Unknown SGO error"))
        return body


def _bool(value: bool | None) -> str | None:
    if value is None:
        return None
    return "true" if value else "false"
