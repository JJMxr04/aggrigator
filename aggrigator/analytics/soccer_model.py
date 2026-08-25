"""Soccer Elo + bivariate Poisson goal model.

No DB cache for v1: recompute from events on demand. EPL has 380 events
per season — walking them is fast enough that adding a cache table is
premature.

Public functions:
- ``compute_elo_for_league`` — walks settled events chronologically and
  returns the current Elo per team plus the per-event rating history.
- ``elo_snapshot_at`` — Elo per team as of strictly before a given
  start_time. Used for pre-match probability computation.
- ``compute_match_probabilities`` — bivariate Poisson over the 0-6
  goals grid; collapses to P(home / draw / away).
- ``compute_form_into_match`` — last-N results for a team strictly
  before a target kickoff.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

# Model constants — tune later. Soccer-only for v1.
MODEL_VERSION = "soccer-elo-poisson-v1"

K_FACTOR = 20
HOME_ADVANTAGE_ELO = 60
INITIAL_ELO = 1500.0

# Bivariate Poisson goal-rate constants.
SCALE = 200.0
HOME_GOAL_ADV = 0.3
GOALS_GRID_MAX = 6  # P(0..6 goals) per side; truncation loss < 0.5% for
                   # typical soccer rates.

# Result codes — match Event.winner_code semantics throughout the codebase.
WINNER_HOME = 1
WINNER_AWAY = 2
WINNER_DRAW = 3


@dataclass
class TeamRating:
    """Single team's Elo state + a tiny per-event history."""
    team_id: str
    current: float = INITIAL_ELO
    season_high: float = INITIAL_ELO
    season_low: float = INITIAL_ELO

    def update(self, new_value: float) -> None:
        self.current = new_value
        if new_value > self.season_high:
            self.season_high = new_value
        if new_value < self.season_low:
            self.season_low = new_value


@dataclass
class EloLeagueState:
    ratings: dict[str, TeamRating] = field(default_factory=dict)
    # Pre-match snapshots — keyed by event_id, value = (home_elo, away_elo)
    # captured just before the event was applied. Used by past-event
    # post-hoc probability rendering.
    pre_match: dict[str, tuple[float, float]] = field(default_factory=dict)

    def rating(self, team_id: str) -> TeamRating:
        r = self.ratings.get(team_id)
        if r is None:
            r = TeamRating(team_id=team_id)
            self.ratings[team_id] = r
        return r


def _expected_home(home_elo: float, away_elo: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((away_elo - home_elo - HOME_ADVANTAGE_ELO) / 400.0))


def _score_home(winner_code: int | None) -> float:
    """Map winner_code to the home team's [0, 1] result for Elo math.

    winner_code: 1 = home win, 2 = away win, 3 = draw, None = unknown.
    """
    if winner_code == WINNER_HOME:
        return 1.0
    if winner_code == WINNER_DRAW:
        return 0.5
    if winner_code == WINNER_AWAY:
        return 0.0
    return 0.5  # safe default for a finalized-but-undetermined event


def apply_event(state: EloLeagueState, event) -> None:
    """Apply one settled event to the running league state.

    Captures the pre-match Elo snapshot before mutating so post-hoc
    probability rendering can use the rating that was in force.
    """
    home_id = event.home_team_id
    away_id = event.away_team_id
    if not home_id or not away_id:
        return

    home = state.rating(home_id)
    away = state.rating(away_id)
    state.pre_match[event.id] = (home.current, away.current)

    expected = _expected_home(home.current, away.current)
    actual = _score_home(event.winner_code)
    delta = K_FACTOR * (actual - expected)
    home.update(home.current + delta)
    away.update(away.current - delta)


def compute_elo_for_league(events: Iterable) -> EloLeagueState:
    """Apply Elo updates over a league's settled events in chronological order.

    Caller is responsible for filtering to ``is_finalized=True`` rows in
    the desired league + season scope. Sorting is done here defensively;
    callers can pass any iterable order.
    """
    state = EloLeagueState()
    rows = sorted(
        (ev for ev in events if ev.start_time is not None),
        key=lambda ev: (ev.start_time, ev.id),
    )
    for ev in rows:
        apply_event(state, ev)
    return state


def elo_snapshot_at(
    state: EloLeagueState,
    event_id: str | None,
    home_team_id: str | None,
    away_team_id: str | None,
) -> tuple[float, float]:
    """Elo as of strictly before a given event.

    For a past event the captured pre-match snapshot is the source of
    truth (so post-hoc probabilities reflect what the model would have
    said *at the time*). For a future event the snapshot doesn't exist
    yet — we fall back to each team's current rating, which is the
    intent: "given everything we know up to now, what does the model
    say about this match."
    """
    if event_id and event_id in state.pre_match:
        return state.pre_match[event_id]
    home_elo = state.ratings[home_team_id].current if home_team_id in state.ratings else INITIAL_ELO
    away_elo = state.ratings[away_team_id].current if away_team_id in state.ratings else INITIAL_ELO
    return home_elo, away_elo


# --- Poisson goal model ----------------------------------------------------


def _poisson_pmf(k: int, mu: float) -> float:
    if mu <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-mu) * (mu ** k) / math.factorial(k)


def league_average_goals(events: Iterable) -> tuple[float, float]:
    """Mean home_score and away_score across settled events.

    Used as the per-side Poisson base rate. Falls back to a generic
    1.4/1.1 when the league has zero settled events — keeps the
    function total even on cold-start leagues.
    """
    home_sum = 0
    away_sum = 0
    n = 0
    for ev in events:
        if ev.home_score is None or ev.away_score is None:
            continue
        home_sum += ev.home_score
        away_sum += ev.away_score
        n += 1
    if n == 0:
        return 1.4, 1.1
    return home_sum / n, away_sum / n


def compute_match_probabilities(
    home_elo: float,
    away_elo: float,
    base_home: float,
    base_away: float,
) -> dict:
    """Return ``{p_home, p_draw, p_away}`` for one matchup.

    Bivariate Poisson over the (0..GOALS_GRID_MAX)^2 grid:
        mu_home = base_home + (elo_home - elo_away)/SCALE + HOME_GOAL_ADV
        mu_away = base_away + (elo_away - elo_home)/SCALE
        P(H=h, A=a) = Poisson(h; mu_home) * Poisson(a; mu_away)
    Sum cells where h > a / h == a / h < a.
    """
    mu_home = max(0.01, base_home + (home_elo - away_elo) / SCALE + HOME_GOAL_ADV)
    mu_away = max(0.01, base_away + (away_elo - home_elo) / SCALE)

    # Precompute per-side PMFs so the grid is O(N) not O(N^2) calls.
    home_pmf = [_poisson_pmf(k, mu_home) for k in range(GOALS_GRID_MAX + 1)]
    away_pmf = [_poisson_pmf(k, mu_away) for k in range(GOALS_GRID_MAX + 1)]

    p_home = p_draw = p_away = 0.0
    for h in range(GOALS_GRID_MAX + 1):
        for a in range(GOALS_GRID_MAX + 1):
            p = home_pmf[h] * away_pmf[a]
            if h > a:
                p_home += p
            elif h == a:
                p_draw += p
            else:
                p_away += p
    # Re-normalize so the three sum to 1 after grid truncation.
    total = p_home + p_draw + p_away
    if total > 0:
        p_home /= total
        p_draw /= total
        p_away /= total
    return {
        "p_home": p_home,
        "p_draw": p_draw,
        "p_away": p_away,
        "mu_home": mu_home,
        "mu_away": mu_away,
    }


# --- Form into a match -----------------------------------------------------


def compute_form_into_match(
    team_id: str,
    events: Iterable,
    *,
    target_start_time: datetime | None,
    last_n: int = 5,
) -> list[str]:
    """Last-N W/D/L letters for a team strictly before ``target_start_time``.

    Returns most-recent-first (so the first letter is the most recent
    result going into the match). Empty list if no priors.
    """
    rows = []
    for ev in events:
        if ev.start_time is None:
            continue
        if target_start_time is not None and ev.start_time >= target_start_time:
            continue
        if ev.home_team_id != team_id and ev.away_team_id != team_id:
            continue
        rows.append(ev)
    rows.sort(key=lambda ev: ev.start_time, reverse=True)
    out: list[str] = []
    for ev in rows[:last_n]:
        out.append(_result_letter_for(team_id, ev))
    return out


def _result_letter_for(team_id: str, ev) -> str:
    if ev.winner_code == WINNER_DRAW:
        return "D"
    if ev.winner_code == WINNER_HOME:
        return "W" if ev.home_team_id == team_id else "L"
    if ev.winner_code == WINNER_AWAY:
        return "W" if ev.away_team_id == team_id else "L"
    return "—"
