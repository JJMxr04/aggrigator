"""``AGG_FORCE_HTTPS`` — the FastAPI equivalent of Django's
SECURE_PROXY_SSL_HEADER (MDProject solved the identical problem that way,
CoreRoot/settings.py).

Topology: browser → Cloudflare (TLS terminates) → cloudflared tunnel →
Traefik **http** entrypoint → app. Traefik doesn't trust cloudflared as a
proxy, so it overwrites ``X-Forwarded-Proto`` to ``http`` — and the app
then builds ``http://`` absolute URLs. Visible symptom: sqladmin's
login-success redirect points at http://, and Chrome blocks the form
submission under CSP ``form-action 'self'`` (an https→http redirect is a
cross-origin downgrade).

Stamping ``scope["scheme"]`` fixes every absolute-URL producer at once
(sqladmin ``url_for`` redirects, ``request.url_for`` in ops templates,
any future one). Only enable when the ONLY public ingress is https —
true here, Cloudflare 301s all plain-http at the edge.
"""

from __future__ import annotations


class ForceHttpsSchemeMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope["scheme"] = "https"
        elif scope["type"] == "websocket":
            scope["scheme"] = "wss"
        await self.app(scope, receive, send)
