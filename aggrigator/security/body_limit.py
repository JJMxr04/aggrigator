"""Request-body size cap (plan §6.3 P1-5).

Pure ASGI middleware — cheap defense in front of Pydantic. Handlers are
schema-safe but parsing a 100 MB JSON body still burns CPU/memory before
validation rejects it; a flat cap removes that class of nuisance.

Two paths:
- ``Content-Length`` present (every real client we serve): compared
  before the app runs; over-limit → immediate 413.
- Chunked/streamed bodies: counted as chunks arrive; on breach the app
  sees a disconnect (request aborted) — we can't cleanly send a 413
  once streaming started, and closing the connection is the standard
  fallback.
"""

from __future__ import annotations

MAX_BODY_BYTES = 1024 * 1024  # 1 MB — largest legitimate body is a slip combine


class BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v for k, v in scope.get("headers", [])}
        raw_cl = headers.get("content-length")
        if raw_cl is not None:
            try:
                if int(raw_cl) > self.max_bytes:
                    return await _send_413(send)
            except ValueError:
                pass  # malformed header — let the server/app reject it

        received = 0

        async def counting_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # Mid-stream breach: surface a disconnect so the app
                    # aborts; the client sees the connection drop.
                    return {"type": "http.disconnect"}
            return message

        return await self.app(scope, counting_receive, send)


async def _send_413(send) -> None:
    body = b'{"detail":"request body too large"}'
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})
