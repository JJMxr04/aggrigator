"""``AGG_TEST_MODE`` gate — used by every endpoint that wipes or rewrites
state in ways that should be impossible in production.

Single source of truth so adding a new dangerous endpoint is one line:

    from aggrigator.ops.testmode import require_test_mode
    @router.post(...)
    async def my_destructive_thing(...):
        require_test_mode()
        ...

Returns nothing on success; raises ``HTTPException(403)`` otherwise.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from aggrigator.config import get_settings


def require_test_mode() -> None:
    """Raise 403 if ``AGG_TEST_MODE`` is False (or unset).

    The default is False — production must opt out, dev must opt in. This
    pairs with admin-auth (the route's existing ``require_admin``
    dependency) — both are necessary, neither is sufficient.
    """
    if not get_settings().test_mode:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=(
                "This endpoint is disabled outside test mode. "
                "Set AGG_TEST_MODE=True in the aggregator's .env to enable "
                "(local dev / staging only — never in production)."
            ),
        )
