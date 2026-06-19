"""Prev/Next record navigation for SQLAdmin edit pages.

SQLAdmin has no built-in record-to-record navigation. This mixin computes
the neighbouring primary keys for the object being edited, following the
view's default sort order (first ``column_default_sort`` entry) with the
primary key as a deterministic tie-break. Two bounded ``LIMIT 1`` queries
keep it cheap on large tables. The overridden ``edit.html`` awaits
``neighbor_pks`` and renders disabled-at-the-ends Prev/Next buttons.

Navigation follows each view's *default* sort, not the operator's live
per-request filter/sort state — that is a deliberate v1 scope boundary.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, literal, or_, select

from aggrigator.db import async_session_factory


class NavigableModelView:
    def _pk_columns(self) -> list:
        return list(self.model.__mapper__.primary_key)

    def _nav_order(self):
        """Return ``(InstrumentedAttribute, descending: bool)`` for the
        navigation key — the first ``column_default_sort`` entry, or the
        primary key ascending when no default sort is set."""
        sort = getattr(self, "column_default_sort", None)
        name: str | None = None
        desc = False
        if isinstance(sort, str):
            name = sort
        elif isinstance(sort, (list, tuple)) and sort:
            first = sort[0]
            if isinstance(first, (list, tuple)):
                name, desc = first[0], bool(first[1])
            elif isinstance(first, str):
                name = first
        if name is None:
            return getattr(self.model, self._pk_columns()[0].name), False
        return getattr(self.model, name), desc

    async def neighbor_pks(self, request, obj) -> dict[str, Any]:
        """``{"prev": pk|None, "next": pk|None}`` for ``obj`` under this
        view's navigation order. Composite-PK models get no neighbours."""
        pk_cols = self._pk_columns()
        if len(pk_cols) != 1:
            return {"prev": None, "next": None}

        pk = getattr(self.model, pk_cols[0].name)
        nav, desc = self._nav_order()
        # Wrap the current row's values in typed literals. SQLAlchemy forbids
        # ``>``/``<`` against a raw Python bool/None (it's usually a mistake),
        # which would crash navigation on views whose first sort key is a
        # Boolean column (e.g. TeamView's match_confirmed) or holds NULL. A
        # typed bindparam sidesteps that guard and keeps NULLs as typed NULLs.
        cur_nav = literal(getattr(obj, nav.key), nav.type)
        cur_pk = literal(getattr(obj, pk_cols[0].name), pk.type)

        # List order is (nav <dir>, pk ASC). "next" = the row immediately
        # after the current one in that order; "prev" = immediately before.
        if not desc:
            after_cond = or_(nav > cur_nav, and_(nav == cur_nav, pk > cur_pk))
            after_order = (nav.asc(), pk.asc())
            before_cond = or_(nav < cur_nav, and_(nav == cur_nav, pk < cur_pk))
            before_order = (nav.desc(), pk.desc())
        else:
            after_cond = or_(nav < cur_nav, and_(nav == cur_nav, pk > cur_pk))
            after_order = (nav.desc(), pk.asc())
            before_cond = or_(nav > cur_nav, and_(nav == cur_nav, pk < cur_pk))
            before_order = (nav.asc(), pk.desc())

        async with async_session_factory() as session:
            nxt = await session.scalar(
                select(pk).where(after_cond).order_by(*after_order).limit(1)
            )
            prv = await session.scalar(
                select(pk).where(before_cond).order_by(*before_order).limit(1)
            )
        return {"prev": prv, "next": nxt}
