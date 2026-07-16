from __future__ import annotations

import math

from spe_prediction.domain import (
    AnalysisResult,
    ExactScoreAnalysis,
    ExactScorePrediction,
    FixtureFeatures,
    MarketOdds,
    OutcomeProbabilities,
    PoissonConfig,
)
from spe_prediction.engine import MatchAnalysisEngine
from spe_prediction.poisson import _dixon_coles_tau, _poisson_pmf


def _outcome_code(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "HOME"
    if home_goals == away_goals:
        return "DRAW"
    return "AWAY"


def _fair_odd(probability: float) -> float:
    return 1.0 / probability if probability > 0 else 999.0


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _raw_score_distribution(
    expected_home_goals: float,
    expected_away_goals: float,
    config: PoissonConfig,
) -> list[ExactScorePrediction]:
    rows: list[ExactScorePrediction] = []
    for home_goals in range(config.max_goals + 1):
        for away_goals in range(config.max_goals + 1):
            probability = (
                _poisson_pmf(home_goals, expected_home_goals)
                * _poisson_pmf(away_goals, expected_away_goals)
                * _dixon_coles_tau(
                    home_goals,
                    away_goals,
                    expected_home_goals,
                    expected_away_goals,
                    config.dixon_coles_rho,
                )
            )
            rows.append(
                ExactScorePrediction(
                    home_goals=home_goals,
                    away_goals=away_goals,
                    probability=probability,
                    fair_odd=_fair_odd(probability),
                    outcome_code=_outcome_code(home_goals, away_goals),
                )
            )
    return rows


def _availability_shape_factor(goals: int, availability_index: float) -> float:
    availability_gap = max(0.0, 1.0 - availability_index)
    if availability_gap <= 0.0:
        return 1.0
    if goals == 0:
        return 1.0 + availability_gap * 0.10
    return max(0.72, 1.0 - availability_gap * 0.16 * goals)


def _context_tail_factor(goals: int, expected_goals: float, goal_delta: float) -> float:
    if abs(goal_delta) < 1e-9:
        return 1.0
    intensity = goals - expected_goals
    return max(0.76, 1.0 + _clamp(goal_delta * 0.32 * intensity, -0.24, 0.24))


def _draw_shape_factor(
    home_goals: int,
    away_goals: int,
    draw_bias: float,
    total_expected_goals: float,
) -> float:
    if home_goals != away_goals:
        return 1.0
    draw_shift = _clamp((draw_bias - 0.27) * 1.8, -0.18, 0.18)
    distance = abs((home_goals + away_goals) - total_expected_goals)
    proximity = max(0.45, 1.0 - 0.18 * distance)
    return max(0.78, 1.0 + draw_shift * proximity)


def _insight_shape_factor(
    home_goals: int,
    away_goals: int,
    insights: tuple[str, ...],
) -> float:
    if not insights:
        return 1.0
    lowered = " ".join(insights).lower()
    total_goals = home_goals + away_goals
    factor = 1.0

    if (
        "over 2.5" in lowered
        or "3 buts ou plus" in lowered
        or "3 buts" in lowered
    ):
        if total_goals >= 3:
            factor *= 1.08
        elif total_goals <= 1:
            factor *= 0.95

    if "btts" in lowered or "les deux equipes marquent" in lowered:
        if home_goals > 0 and away_goals > 0:
            factor *= 1.07
        else:
            factor *= 0.94

    return factor


def _shape_score_distribution(
    rows: list[ExactScorePrediction],
    fixture: FixtureFeatures | None,
    expected_home_goals: float,
    expected_away_goals: float,
) -> list[ExactScorePrediction]:
    if fixture is None:
        return rows

    total_expected_goals = expected_home_goals + expected_away_goals
    home_delta = float(fixture.external_adjustments.get("home_goal_delta", 0.0))
    away_delta = float(fixture.external_adjustments.get("away_goal_delta", 0.0))

    shaped: list[ExactScorePrediction] = []
    for row in rows:
        factor = 1.0
        factor *= _availability_shape_factor(
            row.home_goals,
            fixture.home_team.availability_index,
        )
        factor *= _availability_shape_factor(
            row.away_goals,
            fixture.away_team.availability_index,
        )
        factor *= _context_tail_factor(
            row.home_goals,
            expected_home_goals,
            home_delta,
        )
        factor *= _context_tail_factor(
            row.away_goals,
            expected_away_goals,
            away_delta,
        )
        factor *= _draw_shape_factor(
            row.home_goals,
            row.away_goals,
            fixture.draw_bias,
            total_expected_goals,
        )
        factor *= _insight_shape_factor(
            row.home_goals,
            row.away_goals,
            fixture.insights,
        )

        if total_expected_goals <= 2.0 and (row.home_goals + row.away_goals) >= 5:
            factor *= 0.88
        elif total_expected_goals >= 3.4 and (row.home_goals + row.away_goals) >= 4:
            factor *= 1.05

        probability = row.probability * max(0.05, factor)
        shaped.append(
            ExactScorePrediction(
                home_goals=row.home_goals,
                away_goals=row.away_goals,
                probability=probability,
                fair_odd=_fair_odd(probability),
                outcome_code=row.outcome_code,
            )
        )
    return shaped


def _align_to_outcome_probabilities(
    rows: list[ExactScorePrediction],
    target: OutcomeProbabilities,
) -> tuple[ExactScorePrediction, ...]:
    raw_totals = {
        "HOME": sum(row.probability for row in rows if row.outcome_code == "HOME"),
        "DRAW": sum(row.probability for row in rows if row.outcome_code == "DRAW"),
        "AWAY": sum(row.probability for row in rows if row.outcome_code == "AWAY"),
    }
    target_totals = target.as_dict()

    aligned: list[ExactScorePrediction] = []
    for row in rows:
        bucket_total = raw_totals[row.outcome_code]
        scaled_probability = 0.0
        if bucket_total > 0:
            scaled_probability = row.probability * (target_totals[row.outcome_code] / bucket_total)
        aligned.append(
            ExactScorePrediction(
                home_goals=row.home_goals,
                away_goals=row.away_goals,
                probability=scaled_probability,
                fair_odd=_fair_odd(scaled_probability),
                outcome_code=row.outcome_code,
            )
        )

    total_probability = sum(row.probability for row in aligned)
    if total_probability <= 0:
        return tuple()

    normalized = tuple(
        ExactScorePrediction(
            home_goals=row.home_goals,
            away_goals=row.away_goals,
            probability=row.probability / total_probability,
            fair_odd=_fair_odd(row.probability / total_probability),
            outcome_code=row.outcome_code,
        )
        for row in aligned
    )
    return normalized


def build_exact_score_distribution(
    expected_home_goals: float,
    expected_away_goals: float,
    target_probabilities: OutcomeProbabilities,
    fixture: FixtureFeatures | None = None,
    poisson_config: PoissonConfig | None = None,
    top_n: int = 5,
) -> tuple[tuple[ExactScorePrediction, ...], tuple[ExactScorePrediction, ...]]:
    config = poisson_config or PoissonConfig()
    raw_rows = _raw_score_distribution(
        expected_home_goals,
        expected_away_goals,
        config,
    )
    shaped_rows = _shape_score_distribution(
        raw_rows,
        fixture,
        expected_home_goals,
        expected_away_goals,
    )
    full_distribution = _align_to_outcome_probabilities(
        shaped_rows,
        target_probabilities,
    )
    ordered = tuple(sorted(full_distribution, key=lambda row: row.probability, reverse=True))
    return ordered[:top_n], ordered


def build_exact_score_analysis_from_result(
    fixture: FixtureFeatures,
    match_analysis: AnalysisResult,
    poisson_config: PoissonConfig | None = None,
    top_n: int = 5,
) -> ExactScoreAnalysis:
    top_scores, full_distribution = build_exact_score_distribution(
        match_analysis.expected_home_goals,
        match_analysis.expected_away_goals,
        match_analysis.probabilities,
        fixture=fixture,
        poisson_config=poisson_config,
        top_n=top_n,
    )
    return ExactScoreAnalysis(
        fixture_id=fixture.fixture_id,
        match_analysis=match_analysis,
        top_scores=top_scores,
        full_distribution=full_distribution,
    )


class ExactScoreAgent:
    """Exact-score layer built on top of the main match engine.

    The agent keeps the scoreline shape from the Poisson goal model, then
    rescales home/draw/away buckets so the exact-score grid stays consistent
    with the final 1X2 probabilities of the main engine.
    """

    def __init__(
        self,
        engine: MatchAnalysisEngine | None = None,
        poisson_config: PoissonConfig | None = None,
        top_n: int = 5,
    ) -> None:
        self._engine = engine or MatchAnalysisEngine()
        self._poisson_config = poisson_config or PoissonConfig()
        self._top_n = top_n

    def analyze_fixture(
        self,
        fixture: FixtureFeatures,
        market_odds: MarketOdds | None = None,
        *,
        top_n: int | None = None,
    ) -> ExactScoreAnalysis:
        match_analysis = self._engine.analyze_fixture(fixture, market_odds)
        limit = top_n if top_n is not None else self._top_n
        return build_exact_score_analysis_from_result(
            fixture,
            match_analysis,
            self._poisson_config,
            top_n=limit,
        )
