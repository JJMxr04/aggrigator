"""Idempotency-key derivation for webhook deliveries.

Per plan §4.6: ``f"{event_id}:{sha256(canonical_state_blob)[:16]}"``.

The canonical state blob is a stable JSON serialization of the *load-bearing*
state for that event — anything whose change should re-fire the webhook:

- ``status_type``
- ``home_score`` / ``away_score``
- ``[(selection_id, settlement_status), ...]``  (sorted by selection_id)

Two pure functions:
- ``state_blob_from_specs`` — used at ingest time when MarketSpecs are still
  in memory. Cheap, no DB.
- ``state_blob_from_event`` — used by retests / admin tools that only have
  the persisted event row.

Both produce identical bytes for identical states, so the same event ingested
twice yields the same idempotency key.
"""

from __future__ import annotations

import hashlib
import json
from typing import Iterable

from aggrigator.ingest.normalize import EventSpec


def state_blob_from_specs(spec: EventSpec) -> bytes:
    """Build the canonical state blob from a fresh EventSpec."""
    sels: list[tuple[str, str]] = []
    for market in spec.markets:
        for selection in market.selections:
            # Selections out of normalize haven't been graded yet, so
            # settlement_status is implicitly PENDING. Provider grading
            # happens AFTER write_markets, so the blob captured here only
            # reflects the pre-grade state. Callers that want the post-grade
            # blob must use state_blob_from_event after grading.
            sels.append((selection.selection_id, "PENDING"))
    return _serialize(
        status_type=spec.status_type,
        home_score=spec.home_score,
        away_score=spec.away_score,
        selection_states=sels,
    )


def state_blob_from_event(
    *,
    status_type: str,
    home_score: int | None,
    away_score: int | None,
    selection_states: Iterable[tuple[str, str]],
) -> bytes:
    return _serialize(
        status_type=status_type,
        home_score=home_score,
        away_score=away_score,
        selection_states=list(selection_states),
    )


def state_hash(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()[:16]


def idempotency_key(event_id: str, blob: bytes) -> str:
    return f"{event_id}:{state_hash(blob)}"


# ---- internal --------------------------------------------------------------


def _serialize(
    *,
    status_type: str,
    home_score: int | None,
    away_score: int | None,
    selection_states: list[tuple[str, str]],
) -> bytes:
    """Stable JSON: sorted keys, sorted selection list, no whitespace."""
    selection_states_sorted = sorted(selection_states)
    body = {
        "status_type": status_type,
        "home_score": home_score,
        "away_score": away_score,
        "selections": selection_states_sorted,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
