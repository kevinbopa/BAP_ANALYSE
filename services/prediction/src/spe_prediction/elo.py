from __future__ import annotations

import math

from spe_prediction.domain import EloConfig, EloOutput, FixtureFeatures, OutcomeProbabilities


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def estimate_elo_probabilities(
    fixture: FixtureFeatures,
    config: EloConfig,
) -> EloOutput:
    # Scale the Elo home bonus by the league-specific home_advantage. World Cup
    # and neutral-venue games carry far less home edge than domestic top-flights.
    # Domestic baseline: fixture.home_advantage ~= 0.18 → multiplier 1.0.
    home_scale = max(0.0, min(1.5, fixture.home_advantage / 0.18))
    scaled_home_bonus = config.home_field_bonus * home_scale

    rating_gap = (
        fixture.home_team.elo_rating
        - fixture.away_team.elo_rating
        + scaled_home_bonus
        + (fixture.home_team.recent_form - fixture.away_team.recent_form) * 20.0
        - (fixture.home_team.fatigue_penalty - fixture.away_team.fatigue_penalty) * 25.0
        + (fixture.home_team.availability_index - fixture.away_team.availability_index) * 90.0
    )

    home_share = 1.0 / (1.0 + math.pow(10.0, -rating_gap / config.logistic_scale))
    draw_prob = config.draw_probability_base * math.exp(-abs(rating_gap) / config.draw_decay)
    draw_prob = _clamp(draw_prob + fixture.draw_bias - 0.27, config.draw_probability_floor, config.draw_probability_ceiling)

    win_mass = 1.0 - draw_prob
    home_prob = win_mass * home_share
    away_prob = win_mass * (1.0 - home_share)

    probabilities = OutcomeProbabilities(
        home=home_prob,
        draw=draw_prob,
        away=away_prob,
    )
    return EloOutput(probabilities=probabilities, rating_gap=rating_gap)
