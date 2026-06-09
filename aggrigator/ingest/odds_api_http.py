"""Sync httpx adapter against ``api.odds-api.io/v3``.

Implements the ``OddsClient`` Protocol so the existing orchestrator,
normalizer, and webhook layers see internal-shape payloads — the translation
happens here. See ``odds_api_translate.py`` for the payload mapping rules.

Design notes (full rationale in aggrigator-plan/odds-api/odds-api.md):

- **Sync, not async**: the orchestrator runs HTTP sequentially inside an
  ARQ task; mixing httpx.Client with our existing async DB layer matches
  what sync httpx pattern.
- **Hardcoded bookmaker list** (best-practices.md §4.1): even on paid
  tiers we stay at 2 books — pinning prevents accidental tier-upgrade cost
  bloat and keeps the product surface stable.
- **Header capture** (odds-api.md §5/§6): every response stashes
  ``x-ratelimit-{limit, remaining, reset}`` on the instance so the per-hour
  pacer reads them without an extra round-trip.
- **API-key redaction** (best-practices.md §6): logs strip
  ``apiKey=<value>`` so a debug-level log can never leak the key.
- **/odds/multi batching** (best-practices.md §3): pending events are
  flushed in groups of 10 — the documented batch limit.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterator

import httpx

from aggrigator.ingest.odds_api_errors import (
    InvalidApiKeyError,
    NotFoundError,
    OddsApiError,
    RateLimitExceededError,
    ValidationError,
)
from aggrigator.ingest.odds_api_translate import (
    INTERNAL_TO_ODDSAPI_LEAGUE,
    INTERNAL_TO_ODDSAPI_SPORT,
    ODDSAPI_TO_INTERNAL_SPORT,
    to_internal_event_payload,
)

logger = logging.getLogger(__name__)


# ---- hardcoded bookmaker list (see best-practices.md §4.1) -----------------
# Pinned at 2 — the free-tier cap. Even on paid tiers we keep this list so
# a tier upgrade can't silently expand request volume / DB writes / pricing
# surface. To add a third bookmaker: update this constant + run fixtures.
# This is product, not infra — see best-practices.md §4.1 for the full
# rationale. The two slots are matched to the books selected on the
# odds-api.io account: changing this constant without changing the account
# selection produces HTTP 400 on /odds/multi.
ODDSAPI_BOOKMAKERS: tuple[str, ...] = ("draftkings", "bet365")
# The /odds query param expects display-cased names (e.g. "DraftKings"); our
# internal IDs are lowercase. Map at the API boundary.
_BOOKMAKER_ID_TO_DISPLAY: dict[str, str] = {
    "draftkings": "DraftKings",
    "bet365": "Bet365",
}


# odds-api.io documents 10 events max per /odds/multi call.
_ODDS_MULTI_BATCH_SIZE = 10

# Cap for any single backoff sleep so a runaway header (e.g. Retry-After:
# 86400) can't freeze the cron.
_MAX_RETRY_WAIT = 60.0


_APIKEY_RE = re.compile(r"(apiKey=)[^&\s]+")


def redact_api_key(url_or_msg: str) -> str:
    """Strip ``apiKey=<value>`` from a URL or log message. Cheap defense
    against accidental key leakage in deploy logs / proxies."""
    return _APIKEY_RE.sub(r"\1REDACTED", url_or_msg)


class OddsApiHttpClient:
    """Implements ``OddsClient`` via translation against odds-api.io v3.

    Header state (``last_ratelimit_*``) is captured from every response so
    ``odds_api_quota.quota_status`` can read it without a separate
    ``/account/usage`` call (no such endpoint exists on odds-api.io).
    """

    def __init__(
        self,
        *,
        base_url: str = "https://api.odds-api.io/v3",
        api_key: str = "",
        timeout: float = 30.0,
        max_retries: int = 3,
        bookmakers: tuple[str, ...] = ODDSAPI_BOOKMAKERS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # Connection pool tuning — every cycle issues 1 /events + several
        # /odds/multi calls per league back-to-back to the same host. Without
        # keepalive each call pays ~50-100ms of TLS handshake. With it, only
        # the first call does. ``max_connections`` caps total open sockets
        # if league concurrency is ever turned on.
        # HTTP/2 would be ideal but requires the ``h2`` package which isn't
        # in requirements; add ``http2=True`` here + ``httpx[http2]`` in
        # requirements to multiplex further.
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            limits=httpx.Limits(
                max_keepalive_connections=10,
                max_connections=20,
            ),
            transport=httpx.HTTPTransport(retries=2),
        )
        self.max_retries = max(0, max_retries)
        self.bookmakers = bookmakers
        # Header state, refreshed on every response.
        self.last_ratelimit_limit: int | None = None
        self.last_ratelimit_remaining: int | None = None
        self.last_ratelimit_reset: str | None = None
        # Per-cycle observability counters (read by ops/recorder.py).
        self.request_count: int = 0
        self.error_count: int = 0
        self.rate_limit_hits: int = 0
        self.request_durations_ms: list[int] = []

    def close(self) -> None:
        self.client.close()

    # ---- OddsClient surface ---------------------------------------------

    def get_account_usage(self) -> dict:
        """Synthesize internal-shape usage from our last captured rate-limit
        headers. odds-api.io has no /account/usage endpoint — the live
        headers are the source of truth.

        Returns the minimum shape ``quota.quota_status`` reads. The
        ``per-month`` window is synthesized (cap=unlimited) because
        odds-api.io's metric is per-hour, not per-month — the per-hour
        bucket IS our cap, and the orchestrator's check is wired through
        ``odds_api_quota.py`` rather than this method.
        """
        if self.last_ratelimit_limit is None:
            # Probe with a cheap call so we have header data.
            try:
                self._send("/sports", {})
            except OddsApiError:
                pass
        limit = self.last_ratelimit_limit or 0
        remaining = self.last_ratelimit_remaining
        current = (limit - remaining) if remaining is not None else 0
        tier = "free" if limit and limit <= 100 else "paid"
        return {
            "tier": tier,
            "isActive": True,
            "rateLimits": {
                "per-hour": {
                    "max-requests": limit if limit else "unlimited",
                    "current-requests": current,
                    "reset-at": self.last_ratelimit_reset,
                },
                "per-month": {
                    "max-entities": "unlimited",
                    "current-entities": 0,
                },
            },
        }

    def get_sports(self) -> list[dict]:
        """Pass through odds-api.io /sports as a list of dicts. Mapped to
        sport_id where we have a slug mapping; unknown slugs surface as
        the raw slug uppercased."""
        body = self._send("/sports", {})
        sports = body if isinstance(body, list) else []
        out: list[dict] = []
        for s in sports:
            slug = s.get("slug") or ""
            internal_id = ODDSAPI_TO_INTERNAL_SPORT.get(slug) or slug.upper().replace("-", "_")
            out.append({
                "sportID": internal_id,
                "name": s.get("name") or internal_id.title(),
                "shortName": s.get("name") or internal_id.title(),
            })
        return out

    def get_leagues(self, sport_id: str | None = None) -> list[dict]:
        """Walk one sport's leagues OR every active sport's leagues.

        Only emits leagues that have an entry in
        ``ODDSAPI_TO_INTERNAL_LEAGUE``. Unmapped leagues are logged + dropped
        because the orchestrator walks events keyed on internal league IDs —
        emitting a synthetic ID we don't know how to map back is just
        future-broken data. To add a league: edit
        ``odds_api_translate.INTERNAL_TO_ODDSAPI_LEAGUE``.
        """
        from aggrigator.ingest.odds_api_translate import ODDSAPI_TO_INTERNAL_LEAGUE

        sport_slugs: list[str]
        if sport_id:
            slug = INTERNAL_TO_ODDSAPI_SPORT.get(sport_id)
            if slug is None:
                logger.warning(
                    "oddsapi get_leagues: no slug mapping for sport_id %s — "
                    "returning empty list", sport_id,
                )
                return []
            sport_slugs = [slug]
        else:
            sport_slugs = list(INTERNAL_TO_ODDSAPI_SPORT.values())

        out: list[dict] = []
        dropped: list[str] = []
        for slug in sport_slugs:
            body = self._send("/leagues", {"sport": slug})
            leagues = body if isinstance(body, list) else []
            internal_sport_id = ODDSAPI_TO_INTERNAL_SPORT.get(slug)
            if internal_sport_id is None:
                dropped.append(f"<sport {slug}>")
                continue
            for ln in leagues:
                lslug = ln.get("slug") or ""
                internal_league_id = ODDSAPI_TO_INTERNAL_LEAGUE.get(lslug)
                if internal_league_id is None:
                    dropped.append(lslug)
                    continue
                # Defensive truncation matching column widths
                # (League.id=48, Sport.id=32). Belt + suspenders — the map
                # entries are well under, but a typo shouldn't cause a
                # 500 mid-seed.
                out.append({
                    "leagueID": internal_league_id[:48],
                    "sportID": internal_sport_id[:32],
                    "name": ln.get("name") or internal_league_id,
                    "shortName": (ln.get("name") or internal_league_id)[:48],
                })
        if dropped:
            logger.info(
                "oddsapi get_leagues: dropped %d unmapped league slug(s) — "
                "extend INTERNAL_TO_ODDSAPI_LEAGUE in odds_api_translate.py to "
                "include them. First 20: %s",
                len(dropped), dropped[:20],
            )
        return out

    def get_league_activity(self) -> dict[str, int]:
        """Probe upstream ``/leagues?sport=<slug>`` for every mapped sport,
        return ``{internal_league_id: eventsCount}``.

        Used by the ingest orchestrator to skip walks for offseason
        leagues (eventsCount == 0). One HTTP call per mapped sport — small
        and cacheable upstream, ~free at our call volume.

        Caveats:
          * ``eventsCount`` may include past events (odds-api.md R7); we
            use it only as a *zero-vs-nonzero* signal, not for sizing.
          * On any HTTP failure we return ``{}`` so the orchestrator falls
            back to walking every active league (no false negatives — we
            only skip when upstream explicitly says zero).
        """
        from aggrigator.ingest.odds_api_translate import ODDSAPI_TO_INTERNAL_LEAGUE

        out: dict[str, int] = {}
        for slug in INTERNAL_TO_ODDSAPI_SPORT.values():
            try:
                body = self._send("/leagues", {"sport": slug})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "get_league_activity: /leagues?sport=%s failed (%s) — "
                    "skipping activity probe for this sport.", slug, exc,
                )
                continue
            leagues = body if isinstance(body, list) else []
            for ln in leagues:
                lslug = ln.get("slug") or ""
                internal_league_id = ODDSAPI_TO_INTERNAL_LEAGUE.get(lslug)
                if internal_league_id is None:
                    continue
                count = ln.get("eventsCount")
                if isinstance(count, int):
                    out[internal_league_id] = count
        return out

    def get_teams(self, league_id: str) -> list[dict]:
        """Not supported — odds-api.io's /participants is sport-scoped, not
        league-scoped, and returns players + teams without a clean filter.
        Teams get upserted lazily during event ingest via
        ``upsert_team_from_spec``. Return empty so the seeder finishes
        cleanly."""
        logger.debug(
            "oddsapi get_teams(%s): not supported by provider — "
            "teams created lazily during event ingest", league_id,
        )
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
        """Yield internal-shape payloads for one league's events in window.

        - ``league_id`` is required (we always walk by league, like the
          existing orchestrator does).
        - ``event_id`` short-circuits to a single ``/events/{id}`` lookup.
        - ``odd_ids`` / ``include_*`` / ``bookmaker_id`` / ``limit`` are
          ignored (the OddsClient surface needs them; odds-api.io doesn't).
        - Live events are dropped at the adapter boundary
          (odds-api.md §4.4 scope rule).
        - Settled events yield WITHOUT an /odds call — scores are carried
          on the event payload (odds-api.md §9 cost cut).
        - Pending events are batched 10-at-a-time through /odds/multi.
        """
        if event_id is not None:
            payload = self._get_single_event(event_id)
            if payload is not None:
                yield payload
            return
        if league_id is None:
            return

        sport_id = self._league_to_sport(league_id)
        sport_slug = INTERNAL_TO_ODDSAPI_SPORT.get(sport_id) if sport_id else None
        from aggrigator.ingest.odds_api_translate import INTERNAL_TO_ODDSAPI_LEAGUE
        league_slug = INTERNAL_TO_ODDSAPI_LEAGUE.get(league_id)
        if sport_slug is None or league_slug is None:
            logger.warning(
                "oddsapi get_events: no slug mapping for league %s "
                "(sport_id=%s) — skipping. Update INTERNAL_TO_ODDSAPI_LEAGUE "
                "in odds_api_translate.py.", league_id, sport_id,
            )
            return

        params: dict[str, Any] = {"sport": sport_slug, "league": league_slug}
        if starts_after:
            params["from"] = starts_after
        if starts_before:
            params["to"] = starts_before
        body = self._send("/events", params)
        events = body if isinstance(body, list) else []

        pending_buffer: list[dict] = []
        for ev in events:
            status = ev.get("status")
            if status == "live":
                continue
            if status == "settled":
                # No /odds call — translate event-only.
                translated = self._safe_translate(ev, odds_dict=None)
                if translated is not None:
                    yield translated
                continue
            # status == "pending" — batch /odds/multi calls.
            pending_buffer.append(ev)
            if len(pending_buffer) >= _ODDS_MULTI_BATCH_SIZE:
                yield from self._flush_pending(pending_buffer)
                pending_buffer = []
        if pending_buffer:
            yield from self._flush_pending(pending_buffer)

    def get_event(
        self, event_id: str, *, include_open_close: bool = True,
    ) -> dict | None:
        return self._get_single_event(event_id)

    # ---- internals -----------------------------------------------------

    def _league_to_sport(self, league_id: str) -> str | None:
        """Reverse lookup: league_id → sport_id. Conservative —
        only handles the leagues we map in odds_api_translate.py."""
        # Lazy: walk INTERNAL_TO_ODDSAPI_SPORT to find the right sport. Since the
        # sport_id is encoded in normalize's parse and Event.sport_id is
        # written from translator output, we can use a tiny per-league map.
        # Defaults match the slug map in odds_api_translate.py.
        per_league = {
            "MLB": "BASEBALL",
            "MLS": "SOCCER",
            "NBA": "BASKETBALL",
            "NFL": "FOOTBALL",
            "NHL": "HOCKEY",
            "UEFA_CHAMPIONS_LEAGUE": "SOCCER",
            "EPL": "SOCCER",
            "WNBA": "BASKETBALL",
            "CEBL": "BASKETBALL",
            "BSN": "BASKETBALL",
            "USL_CHAMPIONSHIP": "SOCCER",
            "BRASILEIRAO_SERIE_B": "SOCCER",
            "GAA_FOOTBALL": "GAELIC_FOOTBALL",
            "AIHL": "HOCKEY",
            "VNL": "VOLLEYBALL",
            "VNL_WOMEN": "VOLLEYBALL",
        }
        return per_league.get(league_id)

    def _get_single_event(self, event_id: str) -> dict | None:
        try:
            ev = self._send(f"/events/{event_id}", {})
        except NotFoundError:
            return None
        if not isinstance(ev, dict):
            return None
        if ev.get("status") == "live":
            return None
        odds = None
        if ev.get("status") == "pending":
            try:
                odds = self._send(
                    "/odds",
                    {
                        "eventId": event_id,
                        "bookmakers": self._bookmakers_param(),
                    },
                )
            except NotFoundError:
                odds = None
        return self._safe_translate(ev, odds_dict=odds)

    def _flush_pending(self, events: list[dict]) -> Iterator[dict]:
        """Fetch /odds/multi for the buffered events, then translate each
        with its odds payload."""
        event_ids = [str(e.get("id")) for e in events if e.get("id") is not None]
        if not event_ids:
            return
        try:
            multi = self._send(
                "/odds/multi",
                {
                    "eventIds": ",".join(event_ids),
                    "bookmakers": self._bookmakers_param(),
                },
            )
        except OddsApiError as exc:
            logger.warning(
                "oddsapi /odds/multi failed for %d event(s): %s — "
                "yielding events without odds",
                len(event_ids), exc,
            )
            for ev in events:
                translated = self._safe_translate(ev, odds_dict=None)
                if translated is not None:
                    yield translated
            return

        # /odds/multi returns a list of odds payloads keyed by id. Index for
        # O(1) lookup per event.
        odds_by_id: dict[str, dict] = {}
        if isinstance(multi, list):
            for o in multi:
                if isinstance(o, dict) and o.get("id") is not None:
                    odds_by_id[str(o["id"])] = o

        for ev in events:
            ev_id = str(ev.get("id"))
            translated = self._safe_translate(ev, odds_dict=odds_by_id.get(ev_id))
            if translated is not None:
                yield translated

    def _safe_translate(
        self, event_dict: dict, *, odds_dict: dict | None,
    ) -> dict | None:
        """Translate one event payload, catching SchemaError so a single
        malformed event doesn't kill the cycle."""
        from aggrigator.ingest.odds_api_errors import SchemaError
        try:
            return to_internal_event_payload(event_dict, odds_dict)
        except SchemaError as exc:
            logger.warning(
                "oddsapi translate: skipping event %s — %s",
                event_dict.get("id"), exc,
            )
            return None

    def _bookmakers_param(self) -> str:
        """Display-cased CSV for the /odds and /odds/multi query string.
        odds-api.io expects the display name (``"Bet365"``), our IDs are
        lowercase (``"bet365"``)."""
        return ",".join(
            _BOOKMAKER_ID_TO_DISPLAY.get(b, b.title()) for b in self.bookmakers
        )

    def _send(self, path: str, params: dict[str, Any]) -> Any:
        """One HTTP GET with retry/backoff + header capture. Returns the
        parsed JSON body. Raises ``OddsApiError`` subclasses on failure."""
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        clean["apiKey"] = self.api_key

        backoff = 1.0
        for attempt in range(self.max_retries + 1):
            t0 = time.monotonic()
            try:
                resp = self.client.get(f"/{path.lstrip('/')}", params=clean)
            except httpx.HTTPError as exc:
                self.error_count += 1
                raise OddsApiError(
                    f"GET {path} failed: {redact_api_key(str(exc))}"
                ) from exc
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            self.request_durations_ms.append(elapsed_ms)
            self.request_count += 1
            self._capture_rate_limit_headers(resp)

            redacted_url = redact_api_key(str(resp.request.url))

            if resp.status_code == 429:
                self.rate_limit_hits += 1
                # Prefer Retry-After (seconds), fall back to x-ratelimit-reset
                # (ISO timestamp) so we get an accurate wait even when the
                # server omits Retry-After.
                wait_required = self._parse_retry_after(
                    resp.headers.get("Retry-After")
                )
                wait_source = "Retry-After header"
                if wait_required is None:
                    wait_required = self._seconds_until_reset(
                        resp.headers.get("x-ratelimit-reset")
                    )
                    wait_source = "x-ratelimit-reset header"
                if wait_required is None:
                    wait_required = backoff
                    wait_source = "exponential backoff"

                # Fail fast when the server-mandated wait exceeds our retry
                # budget. Retrying inside this window only burns more
                # requests against an already-exhausted bucket and produces
                # the misleading "after N retries" traceback. The next
                # cron tick will read fresh headers and try again.
                if wait_required > _MAX_RETRY_WAIT:
                    logger.warning(
                        "oddsapi 429 on %s — bucket reset in %.0fs "
                        "(> %.0fs retry cap), failing fast (%s)",
                        path, wait_required, _MAX_RETRY_WAIT, wait_source,
                    )
                    raise RateLimitExceededError(
                        f"oddsapi 429 on {path}: rate limit exhausted, "
                        f"resets in {wait_required:.0f}s "
                        f"(reset_at={self.last_ratelimit_reset})"
                    )

                if attempt >= self.max_retries:
                    logger.warning(
                        "oddsapi 429 on %s after %d retries — giving up",
                        path, attempt,
                    )
                    raise RateLimitExceededError(
                        f"oddsapi 429 on {path} after {attempt} retries"
                    )

                logger.warning(
                    "oddsapi 429 on %s — waiting %.1fs before retry %d/%d (%s)",
                    path, wait_required, attempt + 1, self.max_retries,
                    wait_source,
                )
                time.sleep(wait_required)
                backoff = min(backoff * 2, _MAX_RETRY_WAIT)
                continue

            if resp.status_code == 401:
                self.error_count += 1
                raise InvalidApiKeyError(
                    f"oddsapi 401 on {path}: {redact_api_key(resp.text[:200])}"
                )
            if resp.status_code == 404:
                self.error_count += 1
                raise NotFoundError(
                    f"oddsapi 404 on {path}: {redact_api_key(resp.text[:200])}"
                )
            if resp.status_code == 400:
                self.error_count += 1
                raise ValidationError(
                    f"oddsapi 400 on {path}: {redact_api_key(resp.text[:200])}"
                )
            if resp.status_code >= 400:
                self.error_count += 1
                logger.warning(
                    "oddsapi GET %s → HTTP %d (%dms) — %s",
                    path, resp.status_code, elapsed_ms,
                    redact_api_key(resp.text[:200]),
                )
                raise OddsApiError(
                    f"GET {path} returned {resp.status_code}: "
                    f"{redact_api_key(resp.text[:200])}"
                )

            try:
                body = resp.json()
            except Exception as exc:  # noqa: BLE001
                self.error_count += 1
                raise OddsApiError(
                    f"oddsapi GET {path}: malformed JSON ({exc})"
                ) from exc

            # One INFO line per call so operators can see crons making
            # progress in deploy logs. URL is redacted.
            logger.info(
                "oddsapi GET %s → 200 (%dms, remaining=%s)",
                redacted_url, elapsed_ms,
                self.last_ratelimit_remaining,
            )
            # Every 100 requests, emit usage stats per best-practices.md §1.
            if self.request_count and self.request_count % 100 == 0:
                logger.info(
                    "oddsapi usage: %d req, %d errors, %d 429s, "
                    "remaining=%s/%s",
                    self.request_count, self.error_count, self.rate_limit_hits,
                    self.last_ratelimit_remaining, self.last_ratelimit_limit,
                )
            return body

        raise OddsApiError(f"unreachable: retry loop fell through for {path}")

    def _capture_rate_limit_headers(self, resp: httpx.Response) -> None:
        """Stash ``x-ratelimit-{limit, remaining, reset}`` for the quota
        pacer to read. Tolerant of missing headers."""
        try:
            limit = resp.headers.get("x-ratelimit-limit")
            if limit is not None:
                self.last_ratelimit_limit = int(limit)
        except (TypeError, ValueError):
            pass
        try:
            remaining = resp.headers.get("x-ratelimit-remaining")
            if remaining is not None:
                self.last_ratelimit_remaining = int(remaining)
        except (TypeError, ValueError):
            pass
        reset = resp.headers.get("x-ratelimit-reset")
        if reset is not None:
            self.last_ratelimit_reset = reset

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _seconds_until_reset(value: str | None) -> float | None:
        """Parse ``x-ratelimit-reset`` (ISO-8601 UTC like
        ``2026-05-12T02:22:31Z``) into seconds-from-now. Returns ``None``
        when the header is absent or unparseable so the caller can fall
        back to exponential backoff."""
        if not value:
            return None
        from datetime import datetime, timezone
        # Strip a trailing ``Z`` since fromisoformat() didn't accept it
        # before Python 3.11; harmless on 3.11+.
        s = str(value).strip().rstrip("Z")
        try:
            reset_at = datetime.fromisoformat(s)
        except ValueError:
            return None
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=timezone.utc)
        delta = (reset_at - datetime.now(tz=timezone.utc)).total_seconds()
        return max(0.0, delta)
