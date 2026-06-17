"""GAP-C integration regression: analytics endpoints emit logo_url
via the keyless endpoint, not the never-populated Team.logo_url column.

Covers:
- GET /v1/analytics/teams/{team_id}/summary  → payload["team"]["logo_url"]
- GET /v1/analytics/events/today             → events[*].home_team/away_team.logo_url
"""

from __future__ import annotations

import pytest

from tests.integration.factories import (
    make_event,
    make_league,
    make_sport,
    make_team,
    make_tenant_user,
)

pytestmark = pytest.mark.asyncio

_TEAM_SUMMARY = "/v1/analytics/teams/{team_id}/summary"
_EVENTS_TODAY = "/v1/analytics/events/today"


def _auth(raw_key: str) -> dict:
    return {"X-Aggrigator-Tenant-Key": raw_key}


async def test_team_summary_logo_url_uses_keyless_endpoint(client, session) -> None:
    """team_summary must return logo_url pointing to /v1/teams/{id}/logo,
    not None (which is what the unpopulated Team.logo_url column returns)."""
    sport = await make_sport(session, id="FOOTBALL", name="Football")
    league = await make_league(session, sport=sport, id="NFL", name="NFL")
    team = await make_team(session, league=league, team_id="DAL", name_long="Dallas Cowboys")
    _, raw_key = await make_tenant_user(session, email="logo-test@example.com")

    r = await client.get(
        _TEAM_SUMMARY.format(team_id=team.id),
        headers=_auth(raw_key),
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    logo_url = payload["team"]["logo_url"]
    assert logo_url is not None, "logo_url must not be None"
    assert logo_url.endswith(f"/v1/teams/{team.id}/logo"), (
        f"expected logo_url to end with /v1/teams/{team.id}/logo, got {logo_url!r}"
    )


async def test_events_today_home_team_logo_url_uses_keyless_endpoint(client, session) -> None:
    """events/today must emit logo_url via the keyless endpoint for each team."""
    from datetime import datetime, timedelta, timezone

    from tests.integration.factories import make_market

    sport = await make_sport(session, id="FOOTBALL", name="Football")
    league = await make_league(session, sport=sport, id="NFL", name="NFL")
    home = await make_team(session, league=league, team_id="DAL", name_long="Dallas Cowboys")
    away = await make_team(session, league=league, team_id="PHI", name_long="Philadelphia Eagles")
    event = await make_event(
        session,
        league=league,
        home_team=home,
        away_team=away,
        start_time=datetime.now(tz=timezone.utc) + timedelta(hours=2),
    )
    await make_market(session, event=event, id=f"{event.id}-ml")
    _, raw_key = await make_tenant_user(session, email="logo-events@example.com")

    r = await client.get(_EVENTS_TODAY, headers=_auth(raw_key))
    assert r.status_code == 200, r.text
    events = r.json()["events"]
    assert len(events) >= 1

    ev = next(e for e in events if e["event_id"] == event.id)
    home_logo = ev["home_team"]["logo_url"]
    away_logo = ev["away_team"]["logo_url"]

    assert home_logo is not None, "home_team logo_url must not be None"
    assert home_logo.endswith(f"/v1/teams/{home.id}/logo"), (
        f"expected home logo to end with /v1/teams/{home.id}/logo, got {home_logo!r}"
    )
    assert away_logo is not None, "away_team logo_url must not be None"
    assert away_logo.endswith(f"/v1/teams/{away.id}/logo"), (
        f"expected away logo to end with /v1/teams/{away.id}/logo, got {away_logo!r}"
    )
