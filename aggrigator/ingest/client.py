"""Odds-provider client interface.

The aggregator depends on the ``OddsClient`` Protocol rather than the
concrete HTTP client, so tests can swap in fixtures or stubs without
touching the orchestrator surface.

Concrete implementation: ``OddsApiHttpClient`` (sync httpx against
``api.odds-api.io/v3``). See ``odds_api_http.py``.
"""

from __future__ import annotations

from typing import Iterator, Protocol


class OddsClient(Protocol):
    """Subset of the odds provider surface the aggregator actually uses."""

    def get_account_usage(self) -> dict: ...

    def get_sports(self) -> list[dict]: ...

    def get_leagues(self, sport_id: str | None = None) -> list[dict]: ...

    def get_teams(self, league_id: str) -> list[dict]: ...

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
    ) -> Iterator[dict]: ...

    def get_event(self, event_id: str, *, include_open_close: bool = True) -> dict | None: ...


class OddsClientError(Exception):
    """Any non-recoverable provider failure."""


class QuotaExceeded(OddsClientError):
    """Per-hour rate-limit budget exhausted."""
