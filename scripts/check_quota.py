"""Print the SGO account-usage response and whether the quota guard
would currently short-circuit ingest.

Uses the same config + client the crons use, so the answer matches what
``_run_ingest`` sees when it decides to skip with
``{"reason": "sgo_monthly_quota"}``.

Run with the same env vars the worker has (``SPORTSGAMEODDS_API_KEY``,
``SPORTSGAMEODDS_BASE_URL``, optional ``AGG_SGO_QUOTA_THRESHOLD_PCT``)::

    python scripts/check_quota.py
"""

from __future__ import annotations

import json
import sys

from aggrigator.config import get_settings
from aggrigator.ingest.quota import quota_status
from aggrigator.ingest.sgo_http import SgoHttpClient


def main() -> int:
    settings = get_settings()
    client = SgoHttpClient(
        base_url=settings.sgo_base_url,
        api_key=settings.sgo_api_key,
        min_interval=settings.sgo_min_interval_seconds,
        max_retries=settings.sgo_max_retries,
    )
    print(f"base_url       = {settings.sgo_base_url}")
    print(f"threshold_pct  = {settings.sgo_quota_threshold_pct}")
    print(f"reset_day      = {settings.sgo_quota_reset_day}")
    print(f"pace_floor_pct = {settings.sgo_quota_pace_floor_pct}")
    print()
    try:
        qs = quota_status(
            client,
            threshold_pct=settings.sgo_quota_threshold_pct,
            reset_day=settings.sgo_quota_reset_day,
            pace_floor_pct=settings.sgo_quota_pace_floor_pct,
        )
    finally:
        client.close()

    print("--- /account/usage response ---")
    print(json.dumps(qs.raw, indent=2, sort_keys=True))
    print()
    for line in qs.summary_lines:
        print(line)
    print()
    print(f"exhausted (absolute) = {qs.exhausted}")
    print(f"pace_ok             = {qs.pace_ok}")
    print(f"auto crons would_skip = {qs.should_skip}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
