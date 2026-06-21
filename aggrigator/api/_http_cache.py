"""HTTP caching helpers for read endpoints.

Two cheap wins for clients (MDProject, browsers, any reverse proxy in
between):
  1. ``Cache-Control: public, max-age=N`` — lets the client + any
     intermediary cache the response for N seconds.
  2. ``ETag`` based on a hash of the JSON body — lets the client send
     ``If-None-Match: <etag>`` next time and get a 32-byte 304 instead
     of the full payload.

Usage pattern in an endpoint::

    @router.get("", response_class=Response)
    async def list_events(
        response: Response,
        if_none_match: Annotated[str | None, Header()] = None,
        ...
    ):
        payload = SomeModel(...)
        return cached_json(payload, response, if_none_match, max_age=30)

For endpoints that still want FastAPI's automatic response_model
serialization, use ``apply_cache_headers(response, body_bytes, max_age)``
manually and short-circuit with ``Response(status_code=304)`` if the
caller's ``If-None-Match`` matches the returned ETag.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel


def _compute_etag(body: bytes) -> str:
    # Quoted per RFC 7232 §2.3; md5 is fine for cache validation (not
    # security). Truncate to 32 chars to keep header compact.
    return '"' + hashlib.md5(body, usedforsecurity=False).hexdigest() + '"'  # nosemgrep: python.lang.security.insecure-hash-algorithms-md5.insecure-hash-algorithm-md5


def cached_json(
    payload: Any,
    response: Response,
    if_none_match: str | None,
    *,
    max_age: int,
    public: bool = True,
) -> Response:
    """Render ``payload`` as JSON with ETag + Cache-Control headers.

    ``payload`` may be a pydantic model, a list of models, or any JSON-
    serializable structure. Returns 304 Not Modified when the caller's
    ``If-None-Match`` already matches.
    """
    if isinstance(payload, BaseModel):
        body = payload.model_dump_json().encode("utf-8")
    elif isinstance(payload, list) and payload and isinstance(payload[0], BaseModel):
        body = (
            "[" + ",".join(p.model_dump_json() for p in payload) + "]"
        ).encode("utf-8")
    else:
        body = json.dumps(payload, default=str).encode("utf-8")

    etag = _compute_etag(body)
    visibility = "public" if public else "private"
    cache_control = f"{visibility}, max-age={max_age}"

    if if_none_match == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": cache_control},
        )

    return JSONResponse(
        content=json.loads(body),
        headers={"ETag": etag, "Cache-Control": cache_control},
    )


def apply_cache_headers(
    response: Response, body_bytes: bytes, *, max_age: int, public: bool = True,
) -> str:
    """Lower-level helper: set ETag + Cache-Control on an existing
    response and return the computed ETag so the caller can short-circuit
    with a 304 when ``If-None-Match`` matches.
    """
    etag = _compute_etag(body_bytes)
    visibility = "public" if public else "private"
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = f"{visibility}, max-age={max_age}"
    return etag
