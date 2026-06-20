"""Deterministic provider-catalog construction.

Pure functions (no network, no I/O) shared by the runtime translation layer
and the offline generator script so both agree on how a provider slug maps to
an internal canonical id and a phase-aware match pattern. Regenerate the data
module ``odds_api_catalog.py`` via
``sports-scores-sim/scripts/generate_provider_catalog.py``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# Curated season-phase suffixes. odds-api swaps a league's slug as its season
# moves (``usa-nba`` -> ``usa-nba-playoffs``). EXTEND this when a genuinely new
# phase suffix appears; do NOT add competition/format words (women, doubles,
# apertura, next-pro) — those are distinct leagues, not phases.
PHASE_SUFFIXES: tuple[str, ...] = (
    "playoffs", "postseason", "preseason", "finals", "knockout-stage",
    "group-stage", "qualification", "championship-round", "relegation-round",
    "main-round",
)

PHASE_REGEX_FRAGMENT: str = (
    r"(?:-(?:" + "|".join(PHASE_SUFFIXES) + r"))?"
)


def strip_phase(slug: str) -> str:
    """Drop one trailing known phase suffix, leaving the base slug."""
    for suffix in PHASE_SUFFIXES:
        tail = f"-{suffix}"
        if slug.endswith(tail):
            return slug[: -len(tail)]
    return slug


def derive_canonical_id(base_slug: str, maxlen: int = 48) -> str:
    """``BASE.upper()`` with dashes->underscores; truncate+hash if too long.

    The hash suffix keeps long slugs unique and within the id column width.
    """
    cid = base_slug.upper().replace("-", "_")
    if len(cid) <= maxlen:
        return cid
    digest = hashlib.sha1(base_slug.encode()).hexdigest()[:6].upper()
    return cid[: maxlen - 7] + "_" + digest


def build_league_pattern(base_slug: str) -> re.Pattern[str]:
    """Anchored pattern matching the base slug plus an optional phase suffix."""
    return re.compile(rf"^{re.escape(base_slug)}{PHASE_REGEX_FRAGMENT}$")


@dataclass
class Catalog:
    sport_slugs: dict[str, str]      # canonical_sport_id -> provider sport slug
    league_slugs: dict[str, str]     # canonical_league_id -> provider base slug
    league_to_sport: dict[str, str]  # canonical_league_id -> canonical_sport_id


def build_catalog(
    sports: list[dict],
    leagues_by_sport: dict[str, list[dict]],
    existing: dict | None = None,
) -> Catalog:
    """Transform provider listings into a canonical catalog.

    ``sports`` is the ``/sports`` payload (dicts with ``slug``/``name``).
    ``leagues_by_sport`` maps a provider sport slug to its ``/leagues`` payload.
    ``existing`` optionally carries prior ``sport_slugs``/``league_slugs`` maps
    whose canonical ids are reused so known leagues never get renamed.
    """
    existing = existing or {}
    prior_sport = existing.get("sport_slugs", {})
    prior_league = existing.get("league_slugs", {})
    prior_l2s = existing.get("league_to_sport", {})
    slug_to_existing_sport = {v: k for k, v in prior_sport.items()}
    slug_to_existing_league = {v: k for k, v in prior_league.items()}

    sport_slugs: dict[str, str] = {}
    slug_to_sport_id: dict[str, str] = {}
    for s in sports:
        slug = s.get("slug") or ""
        if not slug:
            continue
        sport_id = slug_to_existing_sport.get(slug) or derive_canonical_id(slug, maxlen=32)
        sport_slugs[sport_id] = slug
        slug_to_sport_id[slug] = sport_id

    # Seed league_slugs and seen_bases from the existing catalog so off-season
    # leagues (absent from this provider run) are never dropped. The provider
    # loop below skips any base already in seen_bases, so phase-variants of a
    # known league collapse onto the existing canonical id, and genuinely new
    # leagues are added normally. Result = union(existing, provider).
    league_slugs: dict[str, str] = dict(prior_league)
    league_to_sport: dict[str, str] = dict(prior_l2s)
    seen_bases: dict[str, str] = {base: lid for lid, base in prior_league.items()}
    for sport_slug, leagues in leagues_by_sport.items():
        sport_id = slug_to_sport_id.get(sport_slug)
        if sport_id is None:
            continue
        for ln in leagues:
            lslug = ln.get("slug") or ""
            if not lslug:
                continue
            base = strip_phase(lslug)
            if base in seen_bases:
                # Fill in league_to_sport if the existing entry lacked it
                # (e.g. existing had league_slugs but no league_to_sport).
                existing_id = seen_bases[base]
                if existing_id not in league_to_sport:
                    league_to_sport[existing_id] = sport_id
                continue
            league_id = slug_to_existing_league.get(base) or derive_canonical_id(base)
            # Guard against a derived-id collision with a different base.
            if league_id in league_slugs and league_slugs[league_id] != base:
                digest = hashlib.sha1(base.encode()).hexdigest()[:6].upper()
                mangled = (league_id[:41] + "_" + digest)[:48]
                if mangled in league_slugs and league_slugs[mangled] != base:
                    raise ValueError(
                        f"Hash-mangled league id {mangled!r} collides: "
                        f"existing base={league_slugs[mangled]!r}, new base={base!r}"
                    )
                league_id = mangled
            seen_bases[base] = league_id
            league_slugs[league_id] = base
            league_to_sport[league_id] = sport_id

    return Catalog(sport_slugs=sport_slugs, league_slugs=league_slugs, league_to_sport=league_to_sport)
