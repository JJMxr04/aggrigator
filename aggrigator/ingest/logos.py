"""Get-or-create team-logo bytes from odds-api into core_team_logo.

The only odds-api caller for logos. ``ensure_team_logo`` is idempotent:
an existing ``ok`` row is returned untouched; a ``missing`` row inside its
cooldown is honored (no refetch); anything else triggers a fetch.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.ingest.odds_api_http import OddsApiHttpClient
from aggrigator.models import Team, TeamLogo

logger = logging.getLogger(__name__)

# odds-api may add a crest later; recheck misses after this window.
LOGO_MISS_COOLDOWN = timedelta(days=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_fresh_miss(row: TeamLogo) -> bool:
    if row.status != "missing":
        return False
    fetched = row.fetched_at
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return _now() - fetched < LOGO_MISS_COOLDOWN


async def ensure_team_logo(
    session: AsyncSession,
    client: OddsApiHttpClient,
    team: Team,
    *,
    force: bool = False,
) -> TeamLogo | None:
    """Ensure a logo row exists for ``team``. Returns the row, or ``None``
    when the team has no ``odds_api_io_key`` to fetch by."""
    row = await session.get(TeamLogo, team.id)
    if row is not None and not force:
        if row.status == "ok":
            return row
        if _is_fresh_miss(row):
            return row

    if not team.odds_api_io_key:
        return row

    result = client.fetch_participant_logo(team.odds_api_io_key)

    if row is None:
        row = TeamLogo(team_id=team.id, status="missing")
        session.add(row)

    if result is None:
        row.image = None
        row.content_type = None
        row.byte_size = 0
        row.etag = None
        row.status = "missing"
        row.source = "odds-api"
        row.fetched_at = _now()
        await session.flush()
        logger.info("logo: team=%s -> missing (odds-api 404)", team.id)
        return row

    data, content_type = result
    row.image = data
    row.content_type = content_type
    row.byte_size = len(data)
    row.etag = hashlib.sha256(data).hexdigest()
    row.status = "ok"
    row.source = "odds-api"
    row.fetched_at = _now()
    await session.flush()
    logger.info("logo: team=%s -> ok (%d bytes)", team.id, row.byte_size)
    return row
