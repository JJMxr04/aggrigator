"""Async factories for domain rows.

Mirrors the role of MDProject's ``core/match/tests/factories.py``: small,
keyword-driven helpers that the integration tests use to seed exactly the
state they need. Each factory commits the row before returning so subsequent
factories can reference it via FK.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.models import (
    Bookmaker,
    BookmakerSelection,
    Event,
    League,
    Market,
    OddsQuote,
    Selection,
    Sport,
    Team,
    User,
    UserRole,
)
from aggrigator.security.passwords import hash_password


async def make_sport(
    session: AsyncSession,
    *,
    id: str = "FOOTBALL",
    name: str = "Football",
    short_name: str = "FB",
    active: bool = True,
) -> Sport:
    row = Sport(id=id, name=name, short_name=short_name, active=active)
    session.add(row)
    await session.commit()
    return row


async def make_league(
    session: AsyncSession,
    *,
    sport: Sport,
    id: str = "NFL",
    name: str = "NFL",
    short_name: str = "NFL",
    active: bool = True,
) -> League:
    row = League(
        id=id, sport_id=sport.id, name=name, short_name=short_name, active=active
    )
    session.add(row)
    await session.commit()
    return row


async def make_team(
    session: AsyncSession,
    *,
    league: League,
    team_id: str = "DAL",
    name_long: str = "Dallas Cowboys",
    name_medium: str = "Cowboys",
    name_short: str = "DAL",
) -> Team:
    row = Team(
        id=Team.synth_pk(league.id, team_id),
        league_id=league.id,
        team_id=team_id,
        sport_id=league.sport_id,
        name_long=name_long,
        name_medium=name_medium,
        name_short=name_short,
    )
    session.add(row)
    await session.commit()
    return row


async def make_event(
    session: AsyncSession,
    *,
    league: League,
    home_team: Team,
    away_team: Team,
    id: str | None = None,
    start_time: datetime | None = None,
    status_type: str = "notstarted",
    is_live: bool = False,
    is_finalized: bool = False,
    completed: bool = False,
    home_score: int | None = None,
    away_score: int | None = None,
    winner_code: int | None = None,
) -> Event:
    row = Event(
        id=id or f"evt-{uuid.uuid4().hex[:12]}",
        sport_id=league.sport_id,
        league_id=league.id,
        type="match",
        status_type=status_type,
        status_display=status_type.title(),
        is_live=is_live,
        is_finalized=is_finalized,
        completed=completed,
        start_time=start_time or datetime.now(tz=timezone.utc) + timedelta(hours=2),
        home_team_id=home_team.id,
        away_team_id=away_team.id,
        home_score=home_score,
        away_score=away_score,
        winner_code=winner_code,
    )
    session.add(row)
    await session.commit()
    return row


async def make_bookmaker(
    session: AsyncSession,
    *,
    id: str = "DRAFTKINGS",
    name: str = "DraftKings",
    active: bool = True,
) -> Bookmaker:
    row = Bookmaker(id=id, name=name, active=active)
    session.add(row)
    await session.commit()
    return row


async def make_market(
    session: AsyncSession,
    *,
    event: Event,
    id: str | None = None,
    category: str = "MONEYLINE",
    type: str = "NFL_POINTS_ML",
    scope: str = "FULL_GAME",
    line: Decimal | None = None,
    side: str = "",
    is_live: bool = False,
    suspended: bool = False,
    last_updated: datetime | None = None,
) -> Market:
    row = Market(
        id=id or f"{event.id}-{type.lower()}",
        event_id=event.id,
        sport_id=event.sport_id,
        category=category,
        type=type,
        scope=scope,
        line=line,
        side=side,
        provider="oddsapi",
        provider_market_id="",
        provider_choice_group="",
        is_live=is_live,
        suspended=suspended,
        last_updated=last_updated or datetime.now(tz=timezone.utc),
    )
    session.add(row)
    await session.commit()
    return row


async def make_selection(
    session: AsyncSession,
    *,
    market: Market,
    type: str = "HOME",
    label: str | None = None,
    decimal_odds: Decimal | None = Decimal("1.5000"),
    opening_decimal_odds: Decimal | None = None,
    settlement_status: str = "PENDING",
    settlement_source: str = "",
) -> Selection:
    row = Selection(
        id=f"{market.id}:{type.lower()}-{type.lower()}",
        market_id=market.id,
        type=type,
        label=label or f"{type} ({market.type})",
        decimal_odds=decimal_odds,
        opening_decimal_odds=opening_decimal_odds,
        settlement_status=settlement_status,
        settlement_source=settlement_source,
    )
    session.add(row)
    await session.commit()
    return row


async def make_quote(
    session: AsyncSession,
    *,
    selection: Selection,
    decimal_odds: Decimal,
    captured_at: datetime | None = None,
) -> OddsQuote:
    row = OddsQuote(
        selection_id=selection.id,
        decimal_odds=decimal_odds,
        captured_at=captured_at or datetime.now(tz=timezone.utc),
    )
    session.add(row)
    await session.commit()
    return row


async def make_book_quote(
    session: AsyncSession,
    *,
    selection: Selection,
    bookmaker: Bookmaker,
    decimal_odds: Decimal,
    available: bool = True,
) -> BookmakerSelection:
    row = BookmakerSelection(
        selection_id=selection.id,
        bookmaker_id=bookmaker.id,
        decimal_odds=decimal_odds,
        available=available,
    )
    session.add(row)
    await session.commit()
    return row


# ---- API client conveniences ----------------------------------------------


async def make_user(
    session: AsyncSession,
    *,
    email: str = "user@example.com",
    password: str = "hunter2hunter2",
    role: str = UserRole.USER,
    is_active: bool = True,
) -> User:
    """Seed a User row directly. The aggrigator no longer exposes self-service
    registration — admin users are seeded out-of-band, and tests use this
    helper instead of hitting a register endpoint."""
    user = User(
        email=email,
        password_hash=hash_password(password),
        role=role,
        is_active=is_active,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def login_and_get_token(
    client,
    session: AsyncSession | None = None,
    *,
    email: str = "user@example.com",
    password: str = "hunter2hunter2",
    role: str = UserRole.USER,
) -> str:
    """Seed a user (if ``session`` is provided) then log in via the API.

    Most data endpoints are public now and don't need a token — keep this
    helper for the auth-protected admin/crons surface and the /v1/auth/me
    round-trip tests.
    """
    if session is not None:
        await make_user(session, email=email, password=password, role=role)
    r = await client.post(
        "/v1/auth/login", json={"email": email, "password": password}
    )
    return r.json()["access_token"]
