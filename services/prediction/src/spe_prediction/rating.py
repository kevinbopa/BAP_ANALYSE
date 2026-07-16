"""Opponent-adjusted Elo ratings.

The synthetic Elo previously derived from a team's own average points is blind
to opposition strength: Jordan topping Asian qualifiers looked as strong as
France topping European ones. This module replays the ENTIRE match history in
chronological order with a real Elo update, so every rating point earned is
proportional to the strength of the opponent it was earned against.

K-factor is scaled by competition importance (a World Cup upset moves ratings
more than a friendly) and margin of victory (a 4-0 says more than a 1-0).
"""
from __future__ import annotations

import math
from typing import Iterable

BASE_RATING = 1500.0
HOME_BONUS_ELO = 50.0
BASE_K = 24.0


def competition_weight(league_name: str) -> float:
    """Hierarchical competition multiplier — canonical version.

    Shared by the Elo K-factor, the layered team metrics and the XGBoost
    training script so every layer agrees on how much a competition matters.
    """
    lowered = (league_name or "").lower()
    if any(kw in lowered for kw in ("u-23", "u23", "u-21", "u21", "u-20", "u20")):
        return 0.65
    if "friendly" in lowered:
        return 0.78
    if "world cup" in lowered and "qualif" not in lowered:
        return 1.30
    if any(kw in lowered for kw in (
        "euro 2", "euro20", "european championship", "copa america",
        "africa cup", "african cup", "afcon", "asian cup", "gold cup",
        "confederations cup", "arab cup",
    )):
        return 1.22
    if "nations league" in lowered:
        return 1.12
    if any(kw in lowered for kw in ("qualification", "qualifier", "wcq")):
        return 1.10
    if "olympic" in lowered:
        return 0.85
    # Coupes continentales de clubs : au-dessus du championnat domestique,
    # sous la Coupe du monde.
    if "champions league" in lowered:
        return 1.18
    if any(kw in lowered for kw in ("europa league", "conference league",
                                    "copa libertadores", "copa sudamericana")):
        return 1.10
    return 1.0


def is_neutral_venue(league_name: str) -> bool:
    """World Cup final tournaments are played on neutral ground for almost
    every participant — no Elo home bonus there."""
    lowered = (league_name or "").lower()
    return "world cup" in lowered and "qualif" not in lowered


def _margin_multiplier(goal_diff: int) -> float:
    if goal_diff <= 1:
        return 1.0
    return 1.0 + 0.4 * math.log1p(goal_diff - 1)


def compute_elo_ratings(
    match_rows: Iterable[tuple],
) -> dict[int, float]:
    """Replay history chronologically and return {team_id: rating}.

    Each row: (kickoff_utc, home_team_id, away_team_id, home_score,
    away_score, league_name). Rows MUST be sorted by kickoff ascending —
    the caller's SQL does that; feeding unsorted rows silently corrupts
    the ratings.
    """
    ratings: dict[int, float] = {}

    for row in match_rows:
        _kickoff, home_id, away_id, home_score, away_score, league_name = row
        if home_score is None or away_score is None:
            continue
        home_id = int(home_id)
        away_id = int(away_id)
        home_score = int(home_score)
        away_score = int(away_score)

        home_rating = ratings.get(home_id, BASE_RATING)
        away_rating = ratings.get(away_id, BASE_RATING)

        bonus = 0.0 if is_neutral_venue(str(league_name or "")) else HOME_BONUS_ELO
        expected_home = 1.0 / (1.0 + math.pow(10.0, -((home_rating - away_rating + bonus) / 400.0)))

        if home_score > away_score:
            actual = 1.0
        elif home_score == away_score:
            actual = 0.5
        else:
            actual = 0.0

        goal_diff = abs(home_score - away_score)
        k = BASE_K * competition_weight(str(league_name or "")) * _margin_multiplier(goal_diff)
        delta = k * (actual - expected_home)

        ratings[home_id] = home_rating + delta
        ratings[away_id] = away_rating - delta

    return ratings
