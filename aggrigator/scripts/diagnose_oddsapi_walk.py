"""Diagnose where odds vanish on an odds-api.io ingest walk.

Pulls one league through the real client + translate + normalize stack
and prints a per-stage row count so we can see EXACTLY where odds get
dropped between "discovered upstream" and "written to PSQL."

Usage::

    AGG_ODDS_PROVIDER=oddsapi ODDSAPI_API_KEY=... \\
      python -m aggrigator.scripts.diagnose_oddsapi_walk --league MLB

If --league is omitted, picks the first League row whose Sport is also
active. No DB writes happen — this only reads the live API + runs the
in-process pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from aggrigator.config import get_settings
from aggrigator.db import session_scope
from aggrigator.ingest.normalize import event_spec_from_payload
from aggrigator.models import League, Sport
from aggrigator.workers.tasks.ingest import _build_client


def _summarize_event(payload: dict, idx: int) -> dict[str, Any]:
    """Return per-event diagnostic counts after translation."""
    ev_id = payload.get("eventID", "?")
    odds = payload.get("odds") or {}
    odd_id_count = len(odds)
    bookmaker_set: set[str] = set()
    for odd in odds.values():
        if not isinstance(odd, dict):
            continue
        for book in (odd.get("byBookmaker") or {}).keys():
            bookmaker_set.add(book)
    spec = event_spec_from_payload(payload)
    market_count = len(spec.markets) if spec else 0
    selection_count = sum(len(m.selections) for m in (spec.markets if spec else []))
    by_book_total = sum(
        len(s.by_bookmaker)
        for m in (spec.markets if spec else [])
        for s in m.selections
    )
    return {
        "idx": idx,
        "event_id": ev_id,
        "status": (payload.get("status") or {}).get("displayLong"),
        "odd_ids_translated": odd_id_count,
        "bookmakers_in_odds": sorted(bookmaker_set),
        "markets_after_normalize": market_count,
        "selections_after_normalize": selection_count,
        "by_bookmaker_rows_total": by_book_total,
    }


async def _pick_league(target: str | None) -> str | None:
    async with session_scope() as session:
        if target:
            row = await session.get(League, target)
            if row is None:
                print(f"[diag] league {target!r} not in DB", file=sys.stderr)
                return None
            return row.id
        result = await session.scalars(
            select(League)
            .join(Sport, League.sport_id == Sport.id)
            .where(League.active.is_(True), Sport.active.is_(True))
            .limit(1)
        )
        row = result.first()
        if row is None:
            print(
                "[diag] no active league found (need League.active AND "
                "Sport.active)",
                file=sys.stderr,
            )
            return None
        return row.id


async def main_async(league_id_arg: str | None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()
    if settings.odds_provider != "oddsapi":
        print(
            f"[diag] AGG_ODDS_PROVIDER={settings.odds_provider!r} — "
            "this script targets oddsapi. Re-run with the env var set.",
            file=sys.stderr,
        )
        return 2

    league_id = await _pick_league(league_id_arg)
    if league_id is None:
        return 1

    now = datetime.now(tz=timezone.utc)
    starts_after = (
        now - timedelta(days=settings.ingest_window_days_behind)
    ).isoformat(timespec="seconds")
    starts_before = (
        now + timedelta(days=settings.ingest_window_days_ahead)
    ).isoformat(timespec="seconds")

    print(f"[diag] league={league_id} window=[{starts_after}, {starts_before}]")
    client = _build_client()
    print(f"[diag] client={type(client).__name__}")

    payloads: list[dict] = list(
        client.get_events(
            league_id=league_id,
            include_open_close=False,
            include_alt_lines=settings.ingest_include_alt_lines,
            odd_ids=None,
            bookmaker_id=None,
            odds_available=None,
            starts_after=starts_after,
            starts_before=starts_before,
            limit=50,
        )
    )
    print(f"[diag] translator yielded {len(payloads)} payload(s)")

    status_counts: Counter[str] = Counter()
    odds_present_counts = 0
    per_event_rows: list[dict] = []
    for idx, p in enumerate(payloads, start=1):
        status_label = (p.get("status") or {}).get("displayLong") or "?"
        status_counts[status_label] += 1
        if p.get("odds"):
            odds_present_counts += 1
        per_event_rows.append(_summarize_event(p, idx))

    print(f"[diag] status breakdown: {dict(status_counts)}")
    print(f"[diag] payloads with non-empty odds dict: {odds_present_counts}")
    if not per_event_rows:
        print("[diag] no payloads to inspect — done.")
        return 0

    sample = per_event_rows[: min(5, len(per_event_rows))]
    print("[diag] per-event drilldown (first 5):")
    for row in sample:
        print(
            f"  #{row['idx']} {row['event_id']} status={row['status']!r}: "
            f"odd_ids={row['odd_ids_translated']} "
            f"books={row['bookmakers_in_odds']} "
            f"markets={row['markets_after_normalize']} "
            f"selections={row['selections_after_normalize']} "
            f"by_book_rows={row['by_bookmaker_rows_total']}"
        )

    totals = {
        "translated_odd_ids": sum(r["odd_ids_translated"] for r in per_event_rows),
        "markets": sum(r["markets_after_normalize"] for r in per_event_rows),
        "selections": sum(r["selections_after_normalize"] for r in per_event_rows),
        "by_bookmaker_rows": sum(
            r["by_bookmaker_rows_total"] for r in per_event_rows
        ),
    }
    print(f"[diag] totals across {len(per_event_rows)} event(s): {totals}")

    if totals["translated_odd_ids"] == 0:
        print(
            "[diag] CONCLUSION: translation produced ZERO odd_ids. "
            "Bug is upstream of normalize — check odds_api_translate "
            "(_translate_odds), bookmaker name mapping, or the "
            "/odds/multi response shape."
        )
    elif totals["markets"] == 0:
        print(
            "[diag] CONCLUSION: odds translated but normalize produced "
            "ZERO markets. Bug is in _market_specs filters (taxonomy.py "
            "BET_TYPE_TO_CATEGORY / PERIOD_TO_SCOPE / statEntityID gate)."
        )
    elif totals["by_bookmaker_rows"] == 0:
        print(
            "[diag] CONCLUSION: markets/selections built but no per-book "
            "quotes. byBookmaker dict reaching normalize._book_quote_specs "
            "is empty — most likely _add_book_quote in odds_api_translate "
            "is dropping every row (bad book_id or unparseable price)."
        )
    else:
        print(
            "[diag] CONCLUSION: pipeline produces non-zero markets + "
            "selections + per-book rows. If PSQL is still empty, the loss "
            "is in the DB write path (savepoint rollback / commit miss / "
            "FK skip in upsert_event_from_spec)."
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--league",
        default=None,
        help="SGO league_id (e.g. MLB). Defaults to first active league.",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args.league))


if __name__ == "__main__":
    raise SystemExit(main())
