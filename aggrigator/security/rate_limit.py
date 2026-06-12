"""Rate limiting — plan §6.2 P0-1, the one genuinely missing battery.

Built directly on the already-pinned ``limits`` package (moving-window
strategy), no slowapi. Two enforcement surfaces:

- ``per_ip(...)`` — a FastAPI dependency for route-level tiers (auth
  endpoints).
- ``rate_limit_dispatch`` — a global middleware tier covering everything
  else: SQLAdmin login (stricter), keyed API traffic (per key+IP bucket),
  and anonymous public traffic (per IP).

Storage comes from ``AGG_RATELIMIT_STORAGE_URL``. Default is in-process
memory — fine for dev/tests, but **advisory ×N in prod**: gunicorn runs
4 uvicorn workers, so memory counters allow 4× the configured limit even
single-replica (and N×4 with replicas). Prod must point this at the
shared Redis resource (``redis://...``) — enforced as a loud boot
warning, not a fatal, so a Redis outage can't take auth down with it.

Tier values are code constants, not env vars — they're security posture,
and posture changes should be diffs, not Coolify edits.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from limits import RateLimitItem, parse
from limits.aio.strategies import MovingWindowRateLimiter
from limits.storage import storage_from_string

logger = logging.getLogger(__name__)

# ---- tiers (plan §6.2) ------------------------------------------------------

#: Login / refresh — brute-force surface. Per IP.
AUTH_RULE: RateLimitItem = parse("5/minute")
#: SQLAdmin login form. Per IP.
ADMIN_LOGIN_RULE: RateLimitItem = parse("5/minute")
#: Anonymous public GETs — protective, not restrictive. Per IP.
PUBLIC_RULE: RateLimitItem = parse("120/minute")
#: Requests presenting a tenant API key — bucketed per (key-prefix, IP) so
#: the MDProject service key gets its own generous lane and junk keys
#: can't ride the anonymous bucket. Key *validity* is enforced later by
#: the auth deps; here an invalid key only buys a bucket, not access.
KEYED_RULE: RateLimitItem = parse("600/minute")

#: Per-account login backoff (complements the per-IP limit against
#: distributed guessing): this many AUTH_LOGIN_FAIL audit rows for one
#: email inside the window → 429 regardless of source IP.
ACCOUNT_BACKOFF_MAX_FAILS = 10
ACCOUNT_BACKOFF_WINDOW = timedelta(minutes=15)

#: Infra probes + channels with their own (stronger) auth story.
_EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/robots.txt"})
_EXEMPT_PREFIXES = ("/v1/internal/",)  # HMAC-signed; MDProject service traffic

_TENANT_KEY_HEADER = "X-Aggrigator-Tenant-Key"

# ---- limiter lifecycle ------------------------------------------------------

_limiter: MovingWindowRateLimiter | None = None
_storage = None


def init_rate_limiter(settings) -> None:
    """Build the storage + strategy once per process. Called from
    create_app(); safe to call again (tests re-import the app module)."""
    global _limiter, _storage
    if not settings.ratelimit_enabled:
        _limiter = None
        _storage = None
        logger.warning("[startup] rate limiting DISABLED (AGG_RATELIMIT_ENABLED=false)")
        return
    url = settings.ratelimit_storage_url or "memory://"
    # The async storage registry namespaces schemes with "async+".
    if not url.startswith("async+"):
        url = f"async+{url}"
    _storage = storage_from_string(url)
    _limiter = MovingWindowRateLimiter(_storage)
    if settings.env in ("prod", "production") and url == "async+memory://":
        logger.warning(
            "[startup] rate-limit storage is in-process memory in prod — "
            "limits are advisory ×N across uvicorn workers. Set "
            "AGG_RATELIMIT_STORAGE_URL=redis://... (shared Coolify Redis)."
        )
    logger.info(
        "[startup] rate limiter ready storage=%s", type(_storage).__name__,
    )


async def reset_for_tests() -> None:
    """Clear every counter. Test isolation only — the ASGI test client
    presents one IP for the whole suite."""
    if _storage is not None:
        await _storage.reset()


async def check(item: RateLimitItem, *identifiers: str) -> int | None:
    """Hit the limiter. Returns ``None`` when allowed, else the number of
    seconds until the moving window frees up (for ``Retry-After``)."""
    if _limiter is None:  # disabled or uninitialized (pure unit tests)
        return None
    if await _limiter.hit(item, *identifiers):
        return None
    stats = await _limiter.get_window_stats(item, *identifiers)
    return max(1, int(stats.reset_time - time.time()) + 1)


def _client_ip(request: Request) -> str:
    # Same source the audit log uses (ops/routes.py, api/auth.py). Behind
    # Traefik this is the edge's view of the client; uvicorn's
    # proxy-headers handling resolves X-Forwarded-For before we see it.
    return request.client.host if request.client else "unknown"


def _http_429(retry_after: int) -> HTTPException:
    # One shape everywhere: reveals nothing about accounts or keys.
    return HTTPException(
        429, "rate limited", headers={"Retry-After": str(retry_after)},
    )


# ---- route-level dependency -------------------------------------------------

def per_ip(item: RateLimitItem, scope: str):
    """Dependency factory: ``Depends(per_ip(AUTH_RULE, "auth"))``."""
    async def dep(request: Request) -> None:
        retry = await check(item, scope, _client_ip(request))
        if retry is not None:
            raise _http_429(retry)
    return dep


# ---- global middleware tier -------------------------------------------------

async def rate_limit_dispatch(request: Request, call_next):
    """Outermost middleware: one bucket decision per request.

    Routes with their own stricter dependency (auth) are *also* counted
    here under the public tier — strictest wins, double-counting a 5/min
    endpoint against a 120/min bucket is harmless.
    """
    if _limiter is None:
        return await call_next(request)
    path = request.url.path
    if path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES):
        return await call_next(request)

    ip = _client_ip(request)
    if request.method == "POST" and path == "/admin/login":
        retry = await check(ADMIN_LOGIN_RULE, "admin-login", ip)
    elif key := request.headers.get(_TENANT_KEY_HEADER):
        # Bucket by key prefix (the indexed public part) + IP. A forged
        # key gets its own bounded lane — it can't exhaust the anonymous
        # bucket, and deps reject it with 401 right after.
        retry = await check(KEYED_RULE, "keyed", key[:24], ip)
    else:
        retry = await check(PUBLIC_RULE, "public", ip)

    if retry is not None:
        return JSONResponse(
            {"detail": "rate limited"},
            status_code=429,
            headers={"Retry-After": str(retry)},
        )
    return await call_next(request)


# ---- per-account login backoff ----------------------------------------------

async def login_backoff_retry_after(session, email: str) -> int | None:
    """429-worthy when ``email`` has accumulated too many AUTH_LOGIN_FAIL
    audit rows inside the window — regardless of source IP, which is what
    the per-IP limiter can't see. Keyed off the *submitted* email whether
    or not the account exists, so the response can't enumerate accounts.

    Returns seconds until the oldest counted failure ages out, or None.
    """
    from sqlalchemy import select

    from aggrigator.models.audit import AuditLog
    from aggrigator.security.audit import Event

    cutoff = datetime.now(tz=timezone.utc) - ACCOUNT_BACKOFF_WINDOW
    created_times = (
        await session.scalars(
            select(AuditLog.created_at)
            .where(
                AuditLog.event_name == Event.AUTH_LOGIN_FAIL,
                AuditLog.created_at >= cutoff,
                AuditLog.audit_metadata["email"].astext == email,
            )
            .order_by(AuditLog.created_at.asc())
        )
    ).all()
    if len(created_times) < ACCOUNT_BACKOFF_MAX_FAILS:
        return None
    oldest = created_times[0]
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    frees_at = oldest + ACCOUNT_BACKOFF_WINDOW
    return max(1, int((frees_at - datetime.now(tz=timezone.utc)).total_seconds()) + 1)
