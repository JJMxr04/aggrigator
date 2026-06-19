"""NavigableModelView.neighbor_pks ordering-key parsing (no DB)."""

from __future__ import annotations

from aggrigator.admin.navigation import NavigableModelView
from aggrigator.models import Sport


class _SportNav(NavigableModelView):
    model = Sport
    column_default_sort = [("name", True)]


class _NoSort(NavigableModelView):
    model = Sport
    column_default_sort = None


def test_nav_order_reads_default_sort():
    col, desc = _SportNav()._nav_order()
    assert col is Sport.name
    assert desc is True


def test_nav_order_falls_back_to_pk():
    col, desc = _NoSort()._nav_order()
    assert col is Sport.id  # primary key
    assert desc is False


def test_entity_views_are_navigable():
    from aggrigator.admin import views
    from aggrigator.admin.navigation import NavigableModelView

    for view_cls in (views.TeamView, views.EventView, views.LeagueView,
                     views.SportView, views.SelectionView, views.BookmakerView,
                     views.UserView):
        assert issubclass(view_cls, NavigableModelView), view_cls.__name__
