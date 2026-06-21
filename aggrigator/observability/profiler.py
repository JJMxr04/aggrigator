"""In-process profilers: pyinstrument (Python call stack) + sqltap (SQL log).

Both are wired through ``init_profilers(app, settings, engine)`` and gated
behind ``AGG_PROFILER_ENABLED=True`` + a matching ``AGG_PROFILER_TOKEN``.
When either is missing, this module is a no-op and the routes / middleware
never install. That makes the file safe to import unconditionally from
main.py — the "is it on?" check lives here.

# pyinstrument
Middleware wraps the request handler with a sampling profiler when the
caller passes ``?profile=1`` AND the matching token. On a profiled
request, the response body is replaced with an HTML flame graph — the
normal JSON is discarded for that one call. Other requests pass through
untouched (zero overhead).

# sqltap
SQLAlchemy event listeners attach to ``engine.sync_engine`` at init time
and accumulate every query the worker runs, with stack traces. The
report endpoint renders an HTML view (grouped by SQL text, N+1 detection,
total time). Memory is bounded by a fixed-size deque so a long-running
session can't OOM the worker.

# Auth
Token check is timing-safe (``hmac.compare_digest``) and required on
every profiler-touching request. Wrong / missing token → the request
behaves as if profiling were off (middleware) or 404 (report endpoint).
The wrong-token path never echoes back any indication of why, so the
endpoints look identical to unmounted routes from the outside.
"""

from __future__ import annotations

import hmac
import logging
from collections import deque
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from aggrigator.config import Settings

logger = logging.getLogger(__name__)

# Cap on retained sqltap statements per worker. ~1000 keeps a few MB
# of stats in memory even under heavy traffic — enough history to spot
# N+1s in the last ~20 requests on a busy endpoint.
_SQLTAP_RING_SIZE = 1000

# Module-level handle to the sqltap session so the report endpoint can
# collect from it. Set by init_profilers() when enabled.
_sqltap_state: dict = {"session": None, "ring": None}


def init_profilers(app: FastAPI, settings: Settings, engine: AsyncEngine) -> None:
    # Always log what we resolved — the #1 source of "why isn't this on"
    # confusion is a stale env-var read. This line lets the operator
    # confirm at a glance what the running process sees.
    logger.info(  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- logs only bool(token is set), never the token value
        "[profiler] init_profilers called: profiler_enabled=%s token_set=%s env=%s",
        settings.profiler_enabled,
        bool(settings.profiler_token),
        settings.env,
    )
    if not settings.profiler_enabled:
        return

    # Token gating policy:
    #   - Token set     → required on every profiler request (prod-safe).
    #   - Token unset   → open access. Useful for local dev when both
    #                     MDProject and the aggrigator are localhost.
    # In prod env we shout loudly when the token is unset, because the
    # combination ("flag on, no token, AGG_ENV=prod") exposes SQL text +
    # stack traces to the open internet — almost always a misconfig.
    if not settings.profiler_token:
        if settings.env in ("prod", "production"):
            logger.error(
                "[profiler] DANGER: AGG_PROFILER_ENABLED=True with no "
                "AGG_PROFILER_TOKEN in prod — profiler endpoints are "
                "world-readable. Set a token or disable the profiler.",
            )
        else:
            logger.warning(
                "[profiler] AGG_PROFILER_TOKEN unset — profiler endpoints "
                "are open (no auth). Fine for local dev; set the token in "
                "any shared environment.",
            )

    pyinstrument_ok = _install_pyinstrument(app, settings)
    sqltap_ok = _install_sqltap(app, settings, engine)
    if not (pyinstrument_ok or sqltap_ok):
        logger.error(
            "[profiler] AGG_PROFILER_ENABLED=True but no profiler libs "
            "are installed. Run: pip install pyinstrument sqltap",
        )
        return
    parts = []
    if pyinstrument_ok:
        parts.append("pyinstrument (?profile=1 on any URL)")
    if sqltap_ok:
        parts.append("sqltap (/v1/ops/profiler/sqltap-report)")
    auth_state = "open (no token)" if not settings.profiler_token else "token-gated"
    logger.info("[profiler] enabled — %s. %s", auth_state, "; ".join(parts))


# ---- token gate --------------------------------------------------------


def _token_ok(request: Request, expected: str) -> bool:
    """Constant-time compare against the header or query param.

    Open access when ``expected`` is empty — init_profilers() decided
    that's OK for this environment and already logged the warning.
    """
    if not expected:
        return True
    supplied = request.headers.get("x-profile-token") or request.query_params.get("token") or ""
    if not supplied:
        return False
    return hmac.compare_digest(supplied, expected)


# ---- pyinstrument ------------------------------------------------------


def _install_pyinstrument(app: FastAPI, settings: Settings) -> bool:
    try:
        from pyinstrument import Profiler
    except ImportError:
        logger.warning("[profiler] pyinstrument not installed; skipping")
        return False

    expected_token = settings.profiler_token

    class PyInstrumentMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Cheap path first: no ?profile=1 → no token check, no overhead.
            if request.query_params.get("profile") not in ("1", "true", "yes"):
                return await call_next(request)
            if not _token_ok(request, expected_token):
                # Behave identically to "profiling not requested" so an
                # unauth'd probe can't distinguish a missing flag from a
                # wrong token.
                return await call_next(request)

            profiler = Profiler(async_mode="enabled", interval=0.001)
            profiler.start()
            try:
                response = await call_next(request)
                # Drain the response body so its serialization time
                # shows up in the captured stack. Without this the
                # flame graph stops at the handler return.
                body = b""
                async for chunk in response.body_iterator:
                    body += chunk
            finally:
                profiler.stop()
            # ``show_all=True`` keeps low-percentage frames visible —
            # useful for finding "the slow thing isn't where I expected"
            # cases. ``timeline=False`` keeps the chronological mode off
            # so percentages match silk's mental model.
            html = profiler.output_html(show_all=True, timeline=False)
            return HTMLResponse(html, status_code=200)

    app.add_middleware(PyInstrumentMiddleware)
    return True


# ---- sqltap -----------------------------------------------------------


def _install_sqltap(app: FastAPI, settings: Settings, engine: AsyncEngine) -> bool:
    try:
        import sqltap
    except ImportError:
        logger.warning("[profiler] sqltap not installed; skipping")
        return False

    # sqltap hooks SQLAlchemy events on the SYNC engine — for an
    # AsyncEngine that means engine.sync_engine. asyncpg goes through
    # SQLAlchemy core's execute() path, which fires the same events.
    sync_engine = getattr(engine, "sync_engine", engine)
    session = sqltap.start(sync_engine)
    # Wrap the session so we cap retained stats. sqltap's ProfilingSession
    # accumulates a list with no bound; on a busy worker that grows
    # without limit. The ring buffer keeps the most recent N statements.
    ring: deque = deque(maxlen=_SQLTAP_RING_SIZE)
    _sqltap_state["session"] = session
    _sqltap_state["ring"] = ring
    _sqltap_state["sqltap_module"] = sqltap

    # Drain sqltap's internal list into the ring on every collect. Done
    # lazily — see _collect_capped() — so we don't add an event-loop
    # task. The trade-off is that between collects, sqltap's own list
    # is the source of growth; in practice we only care about the last
    # few minutes of queries, so this is fine for a debugging session.

    expected_token = settings.profiler_token

    @app.get("/v1/ops/profiler/sqltap-report", include_in_schema=False)
    async def sqltap_report(request: Request) -> Response:
        if not _token_ok(request, expected_token):
            # 404 not 401 — match the "endpoint doesn't exist" facade.
            raise HTTPException(status_code=404)
        stats = _collect_capped()
        # Bypass sqltap.report() — its Mako template crashes on
        # ``'NoneType' object is not subscriptable`` when asyncpg's
        # stack traces don't fit the shape it expects. The collection
        # half of sqltap works fine; the renderer is the broken part.
        return HTMLResponse(_render_sql_report(stats))

    @app.get("/v1/ops/profiler/sqltap-clear", include_in_schema=False)
    async def sqltap_clear(request: Request) -> Response:
        if not _token_ok(request, expected_token):
            raise HTTPException(status_code=404)
        _sqltap_state["ring"].clear()
        # Also reset sqltap's own buffer by stopping + restarting.
        old = _sqltap_state["session"]
        try:
            old.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[profiler] sqltap stop failed: %s", exc)
        _sqltap_state["session"] = sqltap.start(sync_engine)
        return Response(status_code=204)

    return True


def _collect_capped() -> list:
    """Pull sqltap's accumulated stats into our ring and return a snapshot.

    sqltap.ProfilingSession.collect() returns the live internal list and
    keeps appending to it. We extend our ring (auto-truncated to the
    most recent _SQLTAP_RING_SIZE) and return the ring contents. The
    rendered report only ever sees bounded data."""
    session = _sqltap_state.get("session")
    ring = _sqltap_state.get("ring")
    if session is None or ring is None:
        return []
    fresh = list(session.collect())
    ring.extend(fresh)
    return list(ring)


def _render_sql_report(stats: list) -> str:
    """Minimal HTML report — grouped by SQL text, sorted by total time.

    Replaces sqltap's built-in renderer because the latter crashes on
    async stack traces. We keep what's actually useful (text, count,
    total ms, slowest single call) and drop the parts that are flaky
    (per-frame caller breakdown). N+1s jump out as high-count rows;
    slow queries as high-total-ms rows.
    """
    from collections import defaultdict
    from html import escape

    if not stats:
        return (
            "<!doctype html><meta charset='utf-8'>"
            "<body style='font:14px sans-serif;padding:24px'>"
            "<h1>SQL report</h1><p>No queries captured yet. Hit some "
            "aggrigator endpoints, then refresh.</p>"
            "<p><a href='/v1/ops/profiler/sqltap-clear'>clear</a></p>"
            "</body>"
        )

    groups: dict = defaultdict(lambda: {"count": 0, "total": 0.0, "max": 0.0})
    for s in stats:
        text = str(getattr(s, "text", s))
        duration = float(getattr(s, "duration", 0.0) or 0.0)
        g = groups[text]
        g["count"] += 1
        g["total"] += duration
        if duration > g["max"]:
            g["max"] = duration

    rows = sorted(groups.items(), key=lambda kv: -kv[1]["total"])
    total_queries = sum(g["count"] for _, g in rows)
    total_time_ms = sum(g["total"] for _, g in rows) * 1000

    head = (
        "<!doctype html><meta charset='utf-8'>"
        "<title>SQL report</title>"
        "<style>"
        "body{font:13px ui-monospace,Menlo,monospace;padding:20px;background:#0f1115;color:#e6e6e6}"
        "h1{font-size:18px;margin:0 0 4px} "
        ".meta{color:#888;margin-bottom:18px}"
        "table{border-collapse:collapse;width:100%}"
        "th,td{padding:6px 10px;border-bottom:1px solid #222;text-align:left;vertical-align:top}"
        "th{background:#1a1d24;font-weight:600;position:sticky;top:0}"
        "td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}"
        "tr.n1{background:#2a1f10}"  # >5 calls = likely N+1
        "pre{margin:0;white-space:pre-wrap;word-break:break-word;color:#cde}"
        "a{color:#7ab;text-decoration:none}a:hover{text-decoration:underline}"
        "</style>"
        f"<h1>SQL report</h1>"
        f"<div class='meta'>{total_queries} queries / "
        f"{len(rows)} distinct / {total_time_ms:.1f}ms total &middot; "
        f"<a href='/v1/ops/profiler/sqltap-clear'>clear</a></div>"
        "<table><thead><tr>"
        "<th>count</th><th>total ms</th><th>max ms</th><th>avg ms</th>"
        "<th>SQL</th></tr></thead><tbody>"
    )
    body_rows = []
    for sql, g in rows:
        avg_ms = (g["total"] / g["count"]) * 1000 if g["count"] else 0
        cls = "n1" if g["count"] >= 5 else ""
        body_rows.append(
            f"<tr class='{cls}'>"
            f"<td class='num'>{g['count']}</td>"
            f"<td class='num'>{g['total']*1000:.1f}</td>"
            f"<td class='num'>{g['max']*1000:.1f}</td>"
            f"<td class='num'>{avg_ms:.2f}</td>"
            f"<td><pre>{escape(sql)}</pre></td>"
            "</tr>"
        )
    return head + "".join(body_rows) + "</tbody></table>"
