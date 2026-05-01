"""SGO monthly quota probe.

SGO's ``/account/usage`` reports both per-month requests and per-month
entities. Free tiers (e.g. ``amateur``) cap entities — every event +
market returned eats one or more. Hitting the cap mid-month triggers
HTTP 429 on every subsequent ingest, which then surfaces as failed
crons until the calendar rolls over.

This module provides a cheap pre-flight check the cron tasks call before
walking SGO. If we're past the configured threshold on any per-month
cap, the task short-circuits and logs the reason — the next month's
reset re-enables ingest automatically.

Failure modes are intentionally permissive: a metering failure should
never block ingest (we'd rather over-spend than freeze the pipeline on
a transient ``/account/usage`` 500).
"""

from __future__ import annotations

import logging
from typing import Any

from aggrigator.ingest.client import SgoClient

logger = logging.getLogger(__name__)


_PER_MONTH_FIELDS = (
    ("current-requests", "max-requests", "requests"),
    ("current-entities", "max-entities", "entities"),
)


def is_monthly_quota_exhausted(
    client: SgoClient, *, threshold_pct: int = 90,
) -> bool:
    """True if any per-month metered cap is at or past ``threshold_pct``.

    Returns False on missing/unlimited caps or on any error fetching
    ``/account/usage`` — see module docstring for the rationale.
    """
    if threshold_pct >= 100:
        return False  # check disabled

    try:
        usage = client.get_account_usage() or {}
    except Exception as exc:  # noqa: BLE001 — never block ingest on metering
        logger.warning("SGO /account/usage probe failed (proceeding): %s", exc)
        return False

    per_month: dict[str, Any] = (usage.get("rateLimits") or {}).get("per-month") or {}
    if not per_month:
        return False  # simulator / fixture / older SGO response shape

    for cur_key, max_key, label in _PER_MONTH_FIELDS:
        cur = per_month.get(cur_key)
        cap = per_month.get(max_key)
        if cap in (None, "unlimited") or cur is None:
            continue
        try:
            cap_int = int(cap)
            cur_int = int(cur)
        except (TypeError, ValueError):
            continue
        if cap_int <= 0:
            continue
        pct = (cur_int / cap_int) * 100
        if pct >= threshold_pct:
            logger.warning(
                "SGO monthly %s quota at %.1f%% (%s/%s) — over %s%% threshold; skipping ingest",
                label, pct, cur_int, cap_int, threshold_pct,
            )
            return True
    return False
