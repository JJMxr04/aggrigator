"""SGO monthly quota probe + proportional pacer.

SGO's ``/account/usage`` reports both per-month requests and per-month
entities. Free tiers (e.g. ``amateur``) cap entities — every event +
market returned eats one or more. Hitting the cap mid-month triggers
HTTP 429 on every subsequent ingest, which then surfaces as failed
crons until the calendar rolls over.

Two checks live here:

- **Absolute threshold** (``is_monthly_quota_exhausted``): "are we already
  over X% of cap right now?" Trips when usage has already overshot.
- **Proportional pace** (``quota_status.pace_ok``): "if we keep burning
  at the rate we have so far this cycle, will we end the cycle below
  X%?" Trips earlier — typically before the user notices a problem.
  Uses ``reset_day`` to compute "how much of the cycle has elapsed."

Failure modes are intentionally permissive: a metering failure should
never block ingest (we'd rather over-spend than freeze the pipeline on
a transient ``/account/usage`` 500).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from aggrigator.ingest.client import SgoClient

logger = logging.getLogger(__name__)


_PER_MONTH_FIELDS = (
    ("current-requests", "max-requests", "requests"),
    ("current-entities", "max-entities", "entities"),
)

# Windows we surface in the human summary, in order. The skip decision
# is per-month only — the others are informational so operators can
# spot short-burst pressure (per-minute is the 10/min throttle on
# amateur tier; per-hour and per-day catch midday spikes).
_WINDOWS = ("per-month", "per-day", "per-hour", "per-minute")


@dataclass(frozen=True)
class QuotaStatus:
    """Snapshot of the SGO monthly quota.

    ``summary_lines`` is one human-readable line per logical fact (tier,
    reset estimate, each rate-limit window, pace verdict). The worker
    emits each line as its own log entry. ``summary`` joins them with
    ``·`` for one-line consumers.

    ``exhausted`` — True when current usage is already at or past
    threshold (the original guard). Reactive.

    ``pace_ok`` — True when projected end-of-cycle usage stays under
    threshold *given the current burn rate*. False is the proactive
    "you're trending to overshoot" signal. ``pace_reason`` is a short
    explanation either way.

    ``raw`` is the unparsed ``/account/usage`` response so callers can
    dump it for diagnostics without a second HTTP call.
    """
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
        """Auto crons should skip when EITHER the absolute cap is past
        threshold OR the projected pace would overshoot. Manual triggers
        can still call ``is_monthly_quota_exhausted`` directly if they
        want to bypass the pacer."""
        return self.exhausted or not self.pace_ok


def _format_window(window_name: str, window: dict) -> str | None:
    label = window_name.removeprefix("per-")
    parts: list[str] = []
    for current_key, max_key, metric in (
        ("current-requests", "max-requests", "req"),
        ("current-entities", "max-entities", "ent"),
    ):
        cur = window.get(current_key)
        cap = window.get(max_key)
        if cap == "unlimited":
            if cur is not None:
                parts.append(f"{metric}={cur}/∞")
            continue
        if cap is None or cur is None:
            continue
        try:
            cap_int = int(cap)
            cur_int = int(cur)
        except (TypeError, ValueError):
            continue
        if cap_int <= 0:
            continue
        pct = (cur_int / cap_int) * 100
        parts.append(f"{metric}={cur_int}/{cap_int} ({pct:.1f}%)")
    if not parts:
        return None
    return f"{label}: " + " ".join(parts)


def _cycle_bounds_utc(
    now: datetime, reset_day: int,
) -> tuple[datetime, datetime]:
    """Return ``(prior_reset, next_reset)`` UTC datetimes around ``now``.

    Months have variable length — ``reset_day`` is clamped to ``[1, 28]``
    so every month definitely contains the boundary, no Feb-29 surprises.

    Examples (reset_day=3):
      - now = May 7  → (May 3, Jun 3)
      - now = May 1  → (Apr 3, May 3)
      - now = May 3 00:30 → (May 3 00:00, Jun 3 00:00)
    """
    day = max(1, min(28, reset_day))

    candidate_this_month = datetime(
        now.year, now.month, day, tzinfo=timezone.utc,
    )
    if now >= candidate_this_month:
        prior = candidate_this_month
        # advance one month
        if now.month == 12:
            next_ = datetime(now.year + 1, 1, day, tzinfo=timezone.utc)
        else:
            next_ = datetime(now.year, now.month + 1, day, tzinfo=timezone.utc)
    else:
        next_ = candidate_this_month
        # back one month
        if now.month == 1:
            prior = datetime(now.year - 1, 12, day, tzinfo=timezone.utc)
        else:
            prior = datetime(now.year, now.month - 1, day, tzinfo=timezone.utc)
    return prior, next_


def _human_remaining(delta: timedelta) -> str:
    days = delta.days
    hours = (delta.seconds // 3600)
    return f"{days}d {hours}h"


def quota_status(
    client: SgoClient,
    *,
    threshold_pct: int = 90,
    reset_day: int = 1,
    pace_floor_pct: int = 5,
) -> QuotaStatus:
    """One ``/account/usage`` call → structured summary + both guard
    decisions (absolute + proportional).

    Failure modes mirror the rest of this module: any error fetching
    or parsing usage returns ``exhausted=False, pace_ok=True`` and a
    single summary line that explains why we're proceeding blind.
    """
    if threshold_pct >= 100:
        return QuotaStatus(
            summary_lines=["quota check disabled (threshold ≥100%)"],
            exhausted=False,
            pace_ok=True,
            pace_reason="check disabled",
            raw={},
        )

    try:
        usage = client.get_account_usage() or {}
    except Exception as exc:  # noqa: BLE001 — never block ingest on metering
        logger.warning("SGO /account/usage probe failed (proceeding): %s", exc)
        return QuotaStatus(
            summary_lines=[f"quota probe failed ({exc}) — proceeding"],
            exhausted=False,
            pace_ok=True,
            pace_reason="probe failed",
            raw={},
        )

    rate_limits = usage.get("rateLimits") or {}
    per_month: dict[str, Any] = rate_limits.get("per-month") or {}
    if not per_month:
        return QuotaStatus(
            summary_lines=["quota: per-month caps not reported (simulator/fixture?)"],
            exhausted=False,
            pace_ok=True,
            pace_reason="no per-month caps reported",
            raw=usage,
        )

    # ---- account header + cycle ------------------------------------------
    tier = usage.get("tier") or "?"
    is_active = usage.get("isActive")
    active_str = "" if is_active is None else f" · active={'yes' if is_active else 'NO'}"

    now = datetime.now(tz=timezone.utc)
    prior_reset, next_reset = _cycle_bounds_utc(now, reset_day)
    cycle_total = (next_reset - prior_reset).total_seconds()
    cycle_elapsed = (now - prior_reset).total_seconds()
    elapsed_pct = (cycle_elapsed / cycle_total) * 100 if cycle_total > 0 else 0.0
    reset_line = (
        f"cycle: day {reset_day} → day {reset_day} · "
        f"{elapsed_pct:.1f}% elapsed · "
        f"{_human_remaining(next_reset - now)} until reset "
        f"({next_reset.strftime('%Y-%m-%d')} UTC)"
    )

    # ---- exhaustion check (absolute) + pace check (proportional) ---------
    exhausted = False
    pace_ok = True
    pace_reason = "no per-month metric measured"
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
        usage_pct = (cur_int / cap_int) * 100

        # Absolute guard
        if usage_pct >= threshold_pct:
            exhausted = True
            logger.warning(
                "SGO monthly %s quota at %.1f%% (%s/%s) — over %s%% threshold; "
                "skipping ingest",
                label, usage_pct, cur_int, cap_int, threshold_pct,
            )

        # Proportional pace: at this point in the cycle, fair-share
        # usage is ``elapsed_pct × threshold_pct``. Above that, the
        # cron is on track to overshoot. The floor lets a one-time
        # full_refresh at cycle start through.
        budget_pct_now = max(
            float(pace_floor_pct),
            (elapsed_pct * threshold_pct) / 100.0,
        )
        if usage_pct > budget_pct_now:
            # Linear extrapolation of where we'll land at cycle end if
            # we keep burning at the current rate.
            projected_end_pct = (
                (usage_pct / elapsed_pct) * 100.0
                if elapsed_pct > 0 else usage_pct
            )
            pace_ok = False
            pace_reason = (
                f"{label} at {usage_pct:.1f}% with {elapsed_pct:.1f}% of cycle "
                f"elapsed — projected to hit {projected_end_pct:.0f}% by reset "
                f"(budget at this point: {budget_pct_now:.1f}%)"
            )
            logger.warning("SGO pace overshoot: %s", pace_reason)
        elif pace_ok:
            pace_reason = (
                f"{label} {usage_pct:.1f}% ≤ budget {budget_pct_now:.1f}% "
                f"at {elapsed_pct:.1f}% cycle elapsed"
            )

    # ---- compose lines ----------------------------------------------------
    lines = [
        f"account: tier={tier}{active_str}",
        reset_line,
    ]
    for window_name in _WINDOWS:
        window = rate_limits.get(window_name) or {}
        rendered = _format_window(window_name, window)
        if rendered:
            lines.append(rendered)
    lines.append(f"pace: {'OK' if pace_ok else 'SLOW DOWN'} — {pace_reason}")
    if exhausted:
        lines.append(f"⚠ over {threshold_pct}% absolute threshold — ingest will skip")
    elif not pace_ok:
        lines.append(
            f"⚠ projected to overshoot {threshold_pct}% by reset — auto crons will skip"
        )

    return QuotaStatus(
        summary_lines=lines,
        exhausted=exhausted,
        pace_ok=pace_ok,
        pace_reason=pace_reason,
        raw=usage,
    )


def is_monthly_quota_exhausted(
    client: SgoClient, *, threshold_pct: int = 90,
) -> bool:
    """True if any per-month metered cap is at or past ``threshold_pct``.

    Returns False on missing/unlimited caps or on any error fetching
    ``/account/usage`` — see module docstring for the rationale.

    Thin wrapper over ``quota_status`` for callers that want the
    absolute-only check (manual triggers, full_refresh) and don't care
    about the pacing verdict.
    """
    return quota_status(client, threshold_pct=threshold_pct).exhausted
