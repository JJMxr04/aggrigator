"""AGG_FORCE_HTTPS — absolute URLs must come out https behind the
Cloudflare tunnel (the 2026-06-12 admin-login CSP incident: sqladmin's
url_for redirect said http://, Chrome blocked the form submission)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio


async def test_flag_on_absolute_redirects_are_https(monkeypatch) -> None:
    from aggrigator.config import get_settings

    monkeypatch.setattr(get_settings(), "force_https", True)
    # Fresh app so create_app() sees the flag and mounts the middleware.
    from aggrigator.main import create_app

    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        # /admin/logout redirects via sqladmin's url_for — the exact
        # mechanism that produced the http:// Location in prod. The
        # client speaks plain http (like Traefik does); the Location
        # must still be https.
        r = await client.get("/admin/logout")
        assert r.status_code in (302, 303)
        assert r.headers["location"].startswith("https://"), r.headers["location"]


async def test_flag_off_scheme_passthrough() -> None:
    from aggrigator.main import app  # module-level app: flag off in tests

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        r = await client.get("/admin/logout")
        assert r.status_code in (302, 303)
        # Without the override the scheme mirrors the request (http here).
        assert r.headers["location"].startswith("http://"), r.headers["location"]
