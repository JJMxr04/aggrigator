"""Aggregate summary pages for the admin — rollups SQLAdmin can't express
as list views. Each page is a BaseView running one GROUP BY query and
rendering a table. The query functions are split out so they're unit-testable
without going through the HTTP/auth layer."""

from __future__ import annotations

from sqlalchemy import func, select
from sqladmin import BaseView, expose

from aggrigator.db import async_session_factory
from aggrigator.models import Event, League, Team


async def events_by_league(session) -> list[dict]:
    """Event counts per league, broken down by status_type. Sorted by total
    descending so the busiest leagues surface first."""
    rows = (
        await session.execute(
            select(
                League.name.label("league"),
                Event.status_type.label("status"),
                func.count(Event.id).label("n"),
            )
            .select_from(Event)
            .join(League, League.id == Event.league_id)
            .group_by(League.name, Event.status_type)
        )
    ).all()

    agg: dict[str, dict] = {}
    for league, status, n in rows:
        entry = agg.setdefault(league, {"league": league, "total": 0, "by_status": {}})
        entry["by_status"][status] = entry["by_status"].get(status, 0) + n
        entry["total"] += n
    return sorted(agg.values(), key=lambda r: r["total"], reverse=True)


async def teams_by_league(session) -> list[dict]:
    """Team counts per league with a confirmed/unconfirmed split."""
    rows = (
        await session.execute(
            select(
                League.name.label("league"),
                func.count(Team.id).label("total"),
                func.count().filter(Team.match_confirmed.is_(True)).label("confirmed"),
            )
            .select_from(Team)
            .join(League, League.id == Team.league_id)
            .group_by(League.name)
            .order_by(func.count(Team.id).desc())
        )
    ).all()

    return [
        {
            "league": league,
            "total": total,
            "confirmed": confirmed,
            "unconfirmed": total - confirmed,
        }
        for league, total, confirmed in rows
    ]


class EventsByLeagueSummary(BaseView):
    name = "Events by league"
    icon = "fa-solid fa-chart-column"
    category = "Summaries"
    category_icon = "fa-solid fa-layer-group"

    @expose("/summary/events-by-league", methods=["GET"], identity="events-by-league")
    async def page(self, request):
        async with async_session_factory() as session:
            rows = await events_by_league(session)
        statuses = sorted({s for r in rows for s in r["by_status"]})
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/summaries/events_by_league.html",
            {"rows": rows, "statuses": statuses},
        )


class TeamsByLeagueSummary(BaseView):
    name = "Teams by league"
    icon = "fa-solid fa-people-group"
    category = "Summaries"
    category_icon = "fa-solid fa-layer-group"

    @expose("/summary/teams-by-league", methods=["GET"], identity="teams-by-league")
    async def page(self, request):
        async with async_session_factory() as session:
            rows = await teams_by_league(session)
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/summaries/teams_by_league.html",
            {"rows": rows},
        )
