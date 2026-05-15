"""odds-api.io per-hour quota pacer.

odds-api.io meters per-hour requests. The cap is read live from the
``x-ratelimit-limit`` response header — never hardcoded — so the same
code paces correctly on the 100/hr free tier AND on the 5,000/hr
Starter tier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aggrigator.ingest.odds_api_errors import OddsApiError
from aggrigator.ingest.odds_api_http import OddsApiHttpClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuotaStatus:
    summary_lines: list[str]
    exhausted: bool
    pace_ok: bool
    pace_reason: str
    raw: dict[str, Any]

    @property
    def summary(self) -> str:
        return " · ".join(self.summary_lines)

    @property
    def should_skip(self) -> bool:
        return self.exhausted or not self.pace_ok


def quota_status(
    client: OddsApiHttpClient,
    *,
    threshold_pct: int = 80,
) -> QuotaStatus:
    """Read the last captured rate-limit headers from the client; emit a
    skip verdict when current usage is past ``threshold_pct`` of the
    live cap."""
    if threshold_pct >= 100:
        return QuotaStatus(
            summary_lines=["oddsapi quota check disabled (threshold ≥100%)"],
            exhausted=False, pace_ok=True,
            pace_reason="check disabled", raw={},
        )

    # If we haven't observed any header yet (cold start), make one cheap
    # probe call so we have live data. Never block ingest on a probe
    # failure — log + proceed (matches quota.py's "metering failure should
    # never block ingest" stance).
    if client.last_ratelimit_limit is None:
        try:
            client.get_account_usage()
        except OddsApiError as exc:
            logger.warning(
                "oddsapi quota probe failed (proceeding blind): %s", exc,
            )
            return QuotaStatus(
                summary_lines=[f"oddsapi quota probe failed ({exc}) — proceeding"],
                exhausted=False, pace_ok=True,
                pace_reason="probe failed", raw={},
            )

    limit = client.last_ratelimit_limit or 0
    remaining = client.last_ratelimit_remaining
    reset_at = client.last_ratelimit_reset or "?"
    if limit <= 0 or remaining is None:
        return QuotaStatus(
            summary_lines=[
                "oddsapi quota: rate-limit headers not present "
                f"(limit={limit!r}, remaining={remaining!r}) — proceeding"
            ],
            exhausted=False, pace_ok=True,
            pace_reason="no headers", raw={},
        )

    current = limit - remaining
    usage_pct = (current / limit) * 100 if limit else 0.0
    exhausted = usage_pct >= threshold_pct

    tier = "free" if limit <= 100 else "paid"
    lines = [
        f"account: tier={tier} (oddsapi)",
        f"per-hour: req={current}/{limit} ({usage_pct:.1f}%) · reset={reset_at}",
    ]
    if exhausted:
        lines.append(
            f"⚠ over {threshold_pct}% per-hour threshold — ingest will skip"
        )
        pace_reason = (
            f"per-hour at {usage_pct:.1f}% ≥ {threshold_pct}% threshold"
        )
        logger.warning(
            "oddsapi per-hour quota at %.1f%% (%s/%s) — over %s%% threshold; "
            "skipping ingest", usage_pct, current, limit, threshold_pct,
        )
    else:
        pace_reason = (
            f"per-hour {usage_pct:.1f}% ≤ threshold {threshold_pct}%"
        )

    return QuotaStatus(
        summary_lines=lines,
        exhausted=exhausted,
        pace_ok=not exhausted,  # per-hour: same signal as exhausted
        pace_reason=pace_reason,
        raw={
            "limit": limit,
            "remaining": remaining,
            "current": current,
            "reset": reset_at,
        },
    )
