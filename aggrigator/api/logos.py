"""Keyless team-logo bytes. Served from core_team_logo; on a miss the
endpoint lazily fetches from odds-api so downstream consumers (MDProject)
never race the backfill cron. The apiKey stays inside this process.
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Header, Response

from aggrigator.config import get_settings
from aggrigator.deps import SessionDep
from aggrigator.ingest.logos import ensure_team_logo
from aggrigator.ingest.odds_api_http import OddsApiHttpClient
from aggrigator.models import Team, TeamLogo

router = APIRouter(prefix="/v1", tags=["logos"])

logger = logging.getLogger(__name__)

LOGO_MAX_AGE = 86400


def _build_odds_client() -> OddsApiHttpClient:
    settings = get_settings()
    return OddsApiHttpClient(
        api_key=settings.odds_api_key,
        max_retries=settings.odds_api_max_retries,
    )


@router.get("/teams/{team_id}/logo")
async def get_team_logo(
    team_id: str,
    session: SessionDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
):
    row = await session.get(TeamLogo, team_id)

    # Lazy fetch on a real miss (no row, or a stale/expired one).
    if row is None or row.status != "ok":
        team = await session.get(Team, team_id)
        if team is not None:
            client = _build_odds_client()
            try:
                row = await ensure_team_logo(session, client, team)
                await session.commit()
            except httpx.HTTPError as exc:
                # odds-api was unreachable / rate-limited / errored on the
                # lazy-fetch. This is transient, NOT a real "missing" crest —
                # surface 503 (+ Retry-After when odds-api gave one) so the
                # caller backs off and retries, instead of a 500 storm or a
                # poisoned 30-day negative-cache row.
                await session.rollback()
                status_obj = getattr(exc, "response", None)
                retry_after = "60"
                if status_obj is not None:
                    retry_after = status_obj.headers.get("Retry-After", "60")
                logger.warning(
                    "logo lazy-fetch failed for %s: %s", team_id, exc
                )
                return Response(
                    status_code=503, headers={"Retry-After": str(retry_after)}
                )
            finally:
                client.close()

    if row is None or row.status != "ok" or row.image is None:
        return Response(status_code=404)

    headers = {
        "Cache-Control": f"public, max-age={LOGO_MAX_AGE}",
        "ETag": f'"{row.etag}"',
    }
    if if_none_match and if_none_match == f'"{row.etag}"':
        return Response(status_code=304, headers=headers)

    return Response(
        content=row.image,
        media_type=row.content_type or "image/png",
        headers=headers,
    )
