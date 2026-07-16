from __future__ import annotations

import math

from spe_prediction.domain import FixtureFeatures, OutcomeProbabilities, PoissonConfig, PoissonOutput


def _poisson_pmf(goals: int, lam: float) -> float:
    return math.exp(-lam) * (lam**goals) / math.factorial(goals)


def _dixon_coles_tau(home_goals: int, away_goals: int, home_lambda: float, away_lambda: float, rho: float) -> float:
    if home_goals == 0 and away_goals == 0:
        return max(0.01, 1.0 - (home_lambda * away_lambda * rho))
    if home_goals == 0 and away_goals == 1:
        return max(0.01, 1.0 + home_lambda * rho)
    if home_goals == 1 and away_goals == 0:
        return max(0.01, 1.0 + away_lambda * rho)
    if home_goals == 1 and away_goals == 1:
        return max(0.01, 1.0 - rho)
    return 1.0


def estimate_expected_goals(fixture: FixtureFeatures, config: PoissonConfig) -> tuple[float, float]:
    home_attack = fixture.home_team.attack_rating + fixture.home_team.recent_form * 0.15
    away_attack = fixture.away_team.attack_rating + fixture.away_team.recent_form * 0.15

    home_defense_drag = max(0.4, fixture.away_team.defense_rating - fixture.away_team.fatigue_penalty * 0.05)
    away_defense_drag = max(0.4, fixture.home_team.defense_rating - fixture.home_team.fatigue_penalty * 0.05)

    contextual_home = fixture.external_adjustments.get("home_goal_delta", 0.0)
    contextual_away = fixture.external_adjustments.get("away_goal_delta", 0.0)

    expected_home = max(
        0.2,
        config.base_goal_rate
        + config.home_advantage_goals
        + fixture.home_advantage
        + 0.45 * home_attack
        - 0.30 * home_defense_drag
        - 0.12 * fixture.home_team.fatigue_penalty
        - 0.50 * (1.0 - fixture.home_team.availability_index)
        + contextual_home,
    )
    expected_away = max(
        0.2,
        config.base_goal_rate
        + 0.45 * away_attack
        - 0.30 * away_defense_drag
        - 0.12 * fixture.away_team.fatigue_penalty
        - 0.50 * (1.0 - fixture.away_team.availability_index)
        + contextual_away,
    )

    return expected_home, expected_away


def estimate_poisson_probabilities(
    fixture: FixtureFeatures,
    config: PoissonConfig,
) -> PoissonOutput:
    expected_home, expected_away = estimate_expected_goals(fixture, config)

    home_win = 0.0
    draw = 0.0
    away_win = 0.0

    for home_goals in range(config.max_goals + 1):
        for away_goals in range(config.max_goals + 1):
            joint_probability = (
                _poisson_pmf(home_goals, expected_home)
                * _poisson_pmf(away_goals, expected_away)
                * _dixon_coles_tau(home_goals, away_goals, expected_home, expected_away, config.dixon_coles_rho)
            )
            if home_goals > away_goals:
                home_win += joint_probability
            elif home_goals == away_goals:
                draw += joint_probability
            else:
                away_win += joint_probability

    total = home_win + draw + away_win
    probabilities = OutcomeProbabilities(
        home=home_win / total,
        draw=draw / total,
        away=away_win / total,
    )
    return PoissonOutput(
        expected_home_goals=expected_home,
        expected_away_goals=expected_away,
        probabilities=probabilities,
    )
