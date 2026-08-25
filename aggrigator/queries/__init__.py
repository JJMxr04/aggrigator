"""Audited query layer.

All route-facing DB access migrates here so that:

1. Tenant scoping is structural — ``BetQueries`` *cannot* be built
   without a ``tenant_user_id``, and no method returns cross-tenant rows.
2. Raw SQL is bannable — the AST guard test allowlists this package,
   ``ops/`` and ``db.py``; ``text()`` anywhere else fails CI.
3. Filter/pagination parsing lives once, next to the query it feeds.
"""

from aggrigator.queries.bets import BetQueries

__all__ = ["BetQueries"]
