"""Build the outbound v1 webhook payload (plan §4.3) from DB rows.

The orchestrator calls ``build_payload(session, event)`` after a successful
ingest+grade pass; the result is dropped onto a ``WebhookDelivery`` row's
``payload`` column verbatim. Retries re-send the same payload — only the
signature timestamp varies.

All decimals are serialized as strings to preserve precision; all timestamps
are RFC3339 UTC with ``Z`` suffix.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from aggrigator.models import Event, Market, Team

SCHEMA_VERSION = 1


async def build_payload(
    session: AsyncSession,
    event: Event,
    *,
    event_name: str,
    delivery_id: uuid.UUID,
    idempotency_key: str,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    home_team = (
        await session.get(Team, event.home_team_id) if event.home_team_id else None
    )
    away_team = (
        await session.get(Team, event.away_team_id) if event.away_team_id else None
    )
    markets = list(await session.scalars(
        select(Market)
        .where(Market.event_id == event.id)
        .options(selectinload(Market.selections))
        .order_by(Market.category, Market.scope, Market.line)
    ))

    return {
        "schema_version": SCHEMA_VERSION,
        "event_name": event_name,
        "delivery_id": str(delivery_id),
        "idempotency_key": idempotency_key,
        "occurred_at": _iso(occurred_at or datetime.now(tz=timezone.utc)),
        "event": {
            "event_id": event.id,
            "sport_id": event.sport_id,
            "league_id": event.league_id,
            "type": event.type,
            "season_label": event.season_label,
            "start_time": _iso(event.start_time),
            "status_type": event.status_type,
            "status_display": event.status_display,
            "current_period_id": event.current_period_id,
            "is_live": event.is_live,
            "is_finalized": event.is_finalized,
            "completed": event.completed,
            "feed_locked": event.feed_locked,
            "home_team": _team_dict(home_team),
            "away_team": _team_dict(away_team),
            "home_score": event.home_score,
            "away_score": event.away_score,
            "winner_code": event.winner_code,
        },
        "markets": [_market_dict(m) for m in markets],
    }


# ---- internals -------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _dec(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


def _team_dict(team: Team | None) -> dict[str, Any] | None:
    if team is None:
        return None
    return {
        "team_id": team.team_id,
        "league_id": team.league_id,
        "name_long": team.name_long,
        "name_medium": team.name_medium,
        "name_short": team.name_short,
        "primary_color": team.primary_color,
        "stat_entity_id": team.stat_entity_id,
    }


def _market_dict(market: Market) -> dict[str, Any]:
    return {
        "market_id": market.id,
        "category": market.category,
        "type": market.type,
        "scope": market.scope,
        "line": _dec(market.line),
        "side": market.side,
        "subject_team_id": market.subject_team_id,
        "is_live": market.is_live,
        "suspended": market.suspended,
        "selections": [_selection_dict(s) for s in market.selections],
    }


def _selection_dict(selection) -> dict[str, Any]:
    return {
        "selection_id": selection.id,
        "type": selection.type,
        "label": selection.label,
        "decimal_odds": _dec(selection.decimal_odds),
        "opening_decimal_odds": _dec(selection.opening_decimal_odds),
        "movement": selection.movement,
        "settlement_status": selection.settlement_status,
        "settlement_source": selection.settlement_source,
        "settled_at": _iso(selection.settled_at),
    }
