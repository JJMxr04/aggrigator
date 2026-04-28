"""SportsGameOdds oddID taxonomy — verbatim port of MDProject's
``core/event/odds/sgo_taxonomy.py``.

oddID format: ``"{statID}-{statEntityID}-{periodID}-{betTypeID}-{sideID}"``.
"""

from __future__ import annotations

from typing import NamedTuple


BET_TYPE_TO_CATEGORY: dict[str, str] = {
    "ml":     "MONEYLINE",
    "ml3way": "MONEYLINE",
    "sp":     "SPREAD",
    "ou":     "TOTAL",
    "yn":     "PROPS_GAME",
    "eo":     "PROPS_GAME",
}


PERIOD_TO_SCOPE: dict[str, str] = {
    "game":  "FULL_GAME",
    "reg":   "FULL_GAME",
    "1h":    "H1",  "2h": "H2",
    "1q":    "Q1",  "2q": "Q2",  "3q": "Q3",  "4q": "Q4",
    "1p":    "P1",  "2p": "P2",  "3p": "P3",
    "ot":    "OVERTIME",
    "so":    "SHOOTOUT",
    "1i":    "INNING_1",  "2i": "INNING_2",  "3i": "INNING_3",
    "4i":    "INNING_4",  "5i": "INNING_5",  "6i": "INNING_6",
    "7i":    "INNING_7",  "8i": "INNING_8",  "9i": "INNING_9",
    "1st5":  "INNINGS_1_5",
    "1st3":  "INNINGS_1_3",
    "5min":  "MINUTES_5",
    "10min": "MINUTES_10",
}


SIDE_TO_SELECTION: dict[str, str] = {
    "home":  "HOME",
    "away":  "AWAY",
    "draw":  "DRAW",
    "over":  "OVER",
    "under": "UNDER",
    "yes":   "YES",
    "no":    "NO",
    "even":  "EVEN",
    "odd":   "ODD",
}


TYPE_OVERRIDES: dict[tuple[str, str, str, str, str], "TypeOverrideFn"] = {
    ("goals", "all", "game", "yn", "yes"): lambda lg: f"{lg}_BTTS",
    ("goals", "all", "game", "yn", "no"):  lambda lg: f"{lg}_BTTS",
    ("goals", "home", "game", "sp", "home"): lambda lg: f"{lg}_PUCK_LINE" if lg == "NHL" else None,
    ("goals", "away", "game", "sp", "away"): lambda lg: f"{lg}_PUCK_LINE" if lg == "NHL" else None,
    ("runs",  "home", "game", "sp", "home"): lambda lg: f"{lg}_RUN_LINE" if lg == "MLB" else None,
    ("runs",  "away", "game", "sp", "away"): lambda lg: f"{lg}_RUN_LINE" if lg == "MLB" else None,
}


class OddIDParts(NamedTuple):
    statID: str
    statEntityID: str
    periodID: str
    betTypeID: str
    sideID: str


def parse_odd_id(odd_id: str) -> OddIDParts:
    parts = odd_id.split("-")
    if len(parts) != 5:
        raise ValueError(f"Malformed oddID (expected 5 dash-separated parts): {odd_id!r}")
    return OddIDParts(*parts)


def market_type_for(parts: OddIDParts, league_id: str) -> str:
    """``f"{LEAGUE}_{STAT.upper()}_{BETTYPE.upper()}"`` unless overridden."""
    key = (parts.statID, parts.statEntityID, parts.periodID, parts.betTypeID, parts.sideID)
    fn = TYPE_OVERRIDES.get(key)
    if fn is not None:
        override = fn(league_id)
        if override is not None:
            return override
    return f"{league_id}_{parts.statID.upper()}_{parts.betTypeID.upper()}"


TypeOverrideFn = "callable[[str], str | None]"
