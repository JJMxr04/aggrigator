"""SportsGameOdds client interface.

The aggregator never imports the concrete client directly — it depends on
``SGOClient`` (a Protocol). Concrete implementations:

- ``FixtureSGOClient``: reads captured JSON from ``sports-scores/json/sports_game/...``.
  Used in tests and dev. No network. (See ``sgo_simulator.py``.)
- ``SimulatorSGOClient``: HTTP client pointed at the running simulator at
  ``http://127.0.0.1:8765/v2``. (Phase 1.)
- ``SgoHttpClient``: HTTP client against the real ``api.sportsgameodds.com/v2``.
  (Phase 1.)

Switching between them is config-only — same Protocol surface.
"""

from __future__ import annotations

from typing import Iterator, Protocol


class SgoClient(Protocol):
    """Subset of the SGO surface the aggregator actually uses."""

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
        # Single ID or comma-separated list (SGO accepts both, e.g.
        # ``"draftkings,fanduel,betmgm"``). None / empty = all bookmakers.
        bookmaker_id: str | None = None,
        limit: int = 50,
        max_pages: int | None = None,
    ) -> Iterator[dict]: ...

    def get_event(self, event_id: str, *, include_open_close: bool = True) -> dict | None: ...


class SgoError(Exception):
    """Any non-recoverable SGO failure."""


class QuotaExceeded(SgoError):
    """Monthly entity counter past the soft limit."""
