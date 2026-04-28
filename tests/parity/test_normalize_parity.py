"""Parity test — same captured payload through MDProject's normalizer and ours.

If MDProject's normalizer changes, port the change to
``aggrigator/ingest/normalize.py`` and this test will go green again. If this
test goes red, the two pipelines disagree on what to upsert — that's the
production bug we're guarding against.

The MDProject side imports ``core.event.odds.sgo_normalize`` directly. We don't
boot Django because the normalize module is import-pure (no model loading).
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import asdict
from pathlib import Path

import pytest


MDPROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "MDProject"


@pytest.fixture(scope="module")
def md_normalize():
    """Import MDProject's sgo_normalize without booting Django.

    The module declares ``from __future__ import annotations`` and only imports
    ``logging``, ``dataclasses``, ``datetime``, ``decimal``, ``typing``, and its
    sibling pure modules — no Django, no settings.
    """
    if not MDPROJECT_ROOT.exists():
        pytest.skip(f"MDProject not present at {MDPROJECT_ROOT}")
    inserted = False
    if str(MDPROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(MDPROJECT_ROOT))
        inserted = True
    try:
        mod = importlib.import_module("core.event.odds.sgo_normalize")
        return mod
    except ImportError as exc:
        pytest.skip(f"Could not import MDProject normalizer: {exc}")
    finally:
        if inserted:
            sys.path.remove(str(MDPROJECT_ROOT))


def _normalize_spec(spec) -> dict:
    """Convert dataclass spec to a plain dict for cross-implementation compare.

    ``last_updated_at`` (per MarketSpec) is a ``datetime.now()`` snapshot — we
    drop it for the compare since the two normalizers will tick a microsecond
    apart. Everything else is expected to match exactly.
    """
    d = asdict(spec)
    _scrub(d)
    return d


def _scrub(node) -> None:
    if isinstance(node, dict):
        node.pop("last_updated_at", None)
        for v in node.values():
            _scrub(v)
    elif isinstance(node, list):
        for v in node:
            _scrub(v)


def test_event_specs_parity(md_normalize, all_event_payloads):
    """Every captured event must produce identical specs through both normalizers."""
    from aggrigator.ingest.normalize import event_spec_from_payload as us_norm

    them_norm = md_normalize.event_spec_from_payload

    skipped = 0
    compared = 0
    diffs: list[tuple[str, str]] = []
    for payload in all_event_payloads:
        ours = us_norm(payload)
        theirs = them_norm(payload)
        if ours is None and theirs is None:
            skipped += 1
            continue
        assert (ours is None) == (theirs is None), (
            f"event {payload.get('eventID')!r}: one side returned None, the other didn't"
        )
        ours_d = _normalize_spec(ours)
        theirs_d = _normalize_spec(theirs)
        if ours_d != theirs_d:
            diffs.append((payload.get("eventID", "?"), _first_diff(ours_d, theirs_d)))
        compared += 1

    assert compared > 0, "no events compared — fixture corpus empty?"
    assert not diffs, (
        f"normalize parity failed on {len(diffs)} events. First few:\n"
        + "\n".join(f"  {eid}: {info}" for eid, info in diffs[:5])
    )


def _first_diff(a, b, path: str = "") -> str:
    """Walk two nested dict/list structures, return a path:value summary of the
    first difference. Used for human-readable failure output."""
    if type(a) is not type(b):
        return f"{path or '<root>'}: type {type(a).__name__} vs {type(b).__name__}"
    if isinstance(a, dict):
        keys = set(a) | set(b)
        for k in sorted(keys):
            if a.get(k) != b.get(k):
                if k not in a:
                    return f"{path}.{k}: missing in ours"
                if k not in b:
                    return f"{path}.{k}: missing in theirs"
                return _first_diff(a[k], b[k], f"{path}.{k}")
        return ""
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: list length {len(a)} vs {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                return _first_diff(x, y, f"{path}[{i}]")
        return ""
    return f"{path}: {a!r} vs {b!r}"
