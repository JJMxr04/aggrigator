"""neighbor_pks finds correct adjacent PKs under a view's default sort."""

from __future__ import annotations

import pytest

from aggrigator.admin.navigation import NavigableModelView
from aggrigator.models import Sport
from tests.integration.factories import make_sport

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
