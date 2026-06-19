"""neighbor_pks finds correct adjacent PKs under a view's default sort."""

from __future__ import annotations

import pytest

from aggrigator.admin.navigation import NavigableModelView
from aggrigator.models import Sport, Team
from tests.integration.factories import make_league, make_sport, make_team

pytestmark = pytest.mark.asyncio


class _SportNavAsc(NavigableModelView):
    model = Sport
    column_default_sort = [("name", False)]


class _SportNavDesc(NavigableModelView):
    model = Sport
    column_default_sort = [("name", True)]


async def _three_sports(session):
    # Distinct names so the single-key nav order is unambiguous.
    a = await make_sport(session, id="a", name="Alpha")
    b = await make_sport(session, id="b", name="Bravo")
    c = await make_sport(session, id="c", name="Charlie")
    return a, b, c


async def test_middle_row_has_both_neighbors_ascending(session):
    a, b, c = await _three_sports(session)
    out = await _SportNavAsc().neighbor_pks(None, b)
    assert out == {"prev": a.id, "next": c.id}


async def test_first_row_has_no_prev_ascending(session):
    a, _, _ = await _three_sports(session)
    out = await _SportNavAsc().neighbor_pks(None, a)
    assert out["prev"] is None
    assert out["next"] == "b"


async def test_last_row_has_no_next_ascending(session):
    _, _, c = await _three_sports(session)
    out = await _SportNavAsc().neighbor_pks(None, c)
    assert out["prev"] == "b"
    assert out["next"] is None


async def test_descending_reverses_neighbors(session):
    a, b, c = await _three_sports(session)
    out = await _SportNavDesc().neighbor_pks(None, b)
    # Under name DESC the list is Charlie, Bravo, Alpha → next of Bravo is Alpha.
    assert out == {"prev": c.id, "next": a.id}


class _TeamNavByConfirmed(NavigableModelView):
    # First sort key is a BOOLEAN column, mirroring the real TeamView default
    # sort [("match_confirmed", False), ...]. Regression guard for the live
    # 500: SQLAlchemy forbids ``>``/``<`` against a Python bool/None literal,
    # so neighbor_pks must wrap the comparison value in a typed literal().
    model = Team
    column_default_sort = [("match_confirmed", False)]


async def test_boolean_nav_column_does_not_crash(session):
    sport = await make_sport(session, id="soccer", name="Soccer")
    league = await make_league(session, sport=sport, id="epl", name="EPL")
    # Distinct canonical_name avoids the (league_id, canonical_name) collision;
    # pks sort by team_id, so the nav order (match_confirmed ASC, pk ASC) is
    # (False, AAA), (False, BBB), (True, CCC).
    a = await make_team(session, league=league, team_id="AAA", canonical_name="AAA")
    b = await make_team(session, league=league, team_id="BBB", canonical_name="BBB")
    c = await make_team(session, league=league, team_id="CCC", canonical_name="CCC")
    c.match_confirmed = True
    session.add(c)
    await session.commit()

    out = await _TeamNavByConfirmed().neighbor_pks(None, b)
    # Middle row B (False): prev = A (same bool, lower pk), next = C (True).
    assert out == {"prev": a.id, "next": c.id}
