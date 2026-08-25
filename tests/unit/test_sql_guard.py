"""AST guard — the permanent payoff of the query-layer refactor.

Walks every module under ``aggrigator/`` and fails the suite when:

(a) ``text(`` is called outside the audited allowlist (``ops/``,
    ``db.py``, ``queries/``, ``main.py``'s SELECT-1 probe);
(b) ``text()`` receives a non-constant argument (f-string, ``%``,
    ``.format()``, concatenation) anywhere except the allowlist-gated
    ``ops/data_reset.py``;
(c) ``select(`` / ``session.execute(`` appears inside ``api/`` modules
    that have been migrated to the query layer.

There is NO live injection today (2026-06-11 audit) — this guard makes
sure nobody can quietly reintroduce the possibility. If this test is
red on your change: move the query into ``aggrigator/queries/`` (or, if
it's genuinely operational plumbing, extend the allowlist with a
comment explaining why).
"""

from __future__ import annotations

import ast
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[2] / "aggrigator"

# (a)/(b): modules allowed to call sqlalchemy.text() at all.
TEXT_ALLOWED = {
    "db.py",
    "main.py",            # SELECT-1 startup probe
    "ops/",               # advisory locks + allowlist-gated truncates
    "queries/",
}
# (b): the ONLY module allowed to build text() from a non-constant —
# its table names are allowlist-checked against metadata BEFORE
# interpolation (ops/data_reset.py).
DYNAMIC_TEXT_ALLOWED = {"ops/data_reset.py"}

# (c): api/ modules migrated to queries/ — inline ORM is banned here.
# NOT yet migrated (inline selects still allowed, with reasons):
#   api/analytics.py — 1.5k lines of shared-data reads; migrates when the
#       roadmap's Phase 9 (opponent scouting) touches the router anyway.
#   api/auth.py, api/internal.py — auth/provisioning surfaces, not the
#       data plane; trivial keyed lookups.
API_MIGRATED = {
    "api/events.py",
    "api/references.py",
    "api/selections.py",
    "api/bets.py",
}


def _rel(path: Path) -> str:
    return path.relative_to(PKG_ROOT).as_posix()


def _allowed(rel: str, allowlist: set[str]) -> bool:
    return any(rel == entry or rel.startswith(entry) for entry in allowlist)


def _is_text_call(node: ast.Call) -> bool:
    fn = node.func
    return (isinstance(fn, ast.Name) and fn.id == "text") or (
        isinstance(fn, ast.Attribute) and fn.attr == "text"
    )


def _is_constant_str(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_dynamic_string(node: ast.expr) -> bool:
    """f-string, %-format, .format(), or concatenation feeding text()."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)):
        return True
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return True
    return False


def _iter_modules():
    for path in sorted(PKG_ROOT.rglob("*.py")):
        rel = _rel(path)
        tree = ast.parse(path.read_text(), filename=rel)
        yield rel, tree


def test_text_calls_only_in_audited_modules() -> None:
    violations: list[str] = []
    for rel, tree in _iter_modules():
        if _allowed(rel, TEXT_ALLOWED):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_text_call(node):
                violations.append(f"{rel}:{node.lineno} calls text()")
    assert not violations, (
        "raw SQL outside the audited modules — move it into "
        "aggrigator/queries/:\n  " + "\n  ".join(violations)
    )


def test_no_dynamic_sql_strings() -> None:
    violations: list[str] = []
    for rel, tree in _iter_modules():
        if _allowed(rel, DYNAMIC_TEXT_ALLOWED):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_text_call(node)):
                continue
            for arg in node.args:
                if not _is_constant_str(arg) and _is_dynamic_string(arg):
                    violations.append(
                        f"{rel}:{node.lineno} builds SQL from a dynamic string"
                    )
    assert not violations, (
        "string-built SQL is banned everywhere except the allowlist-gated "
        "ops/data_reset.py:\n  " + "\n  ".join(violations)
    )


def test_migrated_api_modules_have_no_inline_orm() -> None:
    violations: list[str] = []
    for rel, tree in _iter_modules():
        if not _allowed(rel, API_MIGRATED):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "select":
                violations.append(f"{rel}:{node.lineno} inline select()")
            elif isinstance(fn, ast.Attribute) and fn.attr == "execute":
                violations.append(f"{rel}:{node.lineno} session.execute()")
    assert not violations, (
        "migrated api/ modules must go through aggrigator/queries/:\n  "
        + "\n  ".join(violations)
    )


def test_guard_catches_a_planted_fstring_text() -> None:
    """Self-test: the dynamic-string detector actually detects."""
    planted = ast.parse('text(f"TRUNCATE {evil}")')
    call = planted.body[0].value
    assert _is_text_call(call)
    assert _is_dynamic_string(call.args[0])
