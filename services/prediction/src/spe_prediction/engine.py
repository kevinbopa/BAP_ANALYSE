from __future__ import annotations

from typing import Protocol

from spe_prediction.decision import build_selection_decisions
from spe_prediction.domain import (
    AnalysisResult,
    DecisionConfig,
    EloOutput,
    EngineConfig,
    FixtureFeatures,
    ImpliedProbabilities,
    MarketOdds,
    OutcomeProbabilities,
)
from spe_prediction.calibration import load_calibration, sharpen
from spe_prediction.elo import estimate_elo_probabilities
from spe_prediction.explain import build_explanation
from spe_prediction.forecaster import build_verdict
from spe_prediction.market import remove_overround
from spe_prediction.poisson import estimate_poisson_probabilities


class XGBoostLike(Protocol):
    @property
    def available(self) -> bool: ...

    def predict(
        self,
        fixture: FixtureFeatures,
        market_odds: MarketOdds | None,
    ) -> OutcomeProbabilities | None: ...


def _market_reliability(market_odds: MarketOdds | None) -> float:
    if market_odds is None:
        return 0.0
    coverage = min(1.0, market_odds.source_count / 12.0)
    average_spread = (market_odds.home_spread + market_odds.draw_spread + market_odds.away_spread) / 3.0
    agreement = max(0.0, 1.0 - (average_spread / 0.30))
    return max(0.0, min(1.0, 0.4 * coverage + 0.6 * agreement))


def _team_reliability(fixture: FixtureFeatures) -> float:
    return (fixture.home_team.data_quality + fixture.away_team.data_quality) / 2.0


def _adaptive_market_weight(
    team_reliability: float,
    market_reliability: float,
    base_anchor: float,
) -> float:
    if market_reliability <= 0.0:
        return 0.0
    history_gap = max(0.0, 1.0 - team_reliability)
    # When market agreement is high and team history is empty, defer almost
    # entirely to the market — leaving room only for genuine model dissent
    # (which we expect once team history fills in).
    if market_reliability >= 0.80:
        ceiling = 0.95
    elif market_reliability >= 0.60:
        ceiling = 0.85
    else:
        ceiling = 0.70
    return min(ceiling, base_anchor + history_gap * (ceiling - base_anchor) * market_reliability)


def _model_shares(
    config: DecisionConfig,
    xgboost_available: bool,
) -> tuple[float, float, float]:
    """Return (poisson_share, elo_share, xgb_share) summing to 1.0.

    When XGBoost is unavailable we keep the configured 2-way split
    (Poisson / Elo). When it is available we re-distribute the model mass
    to give XGBoost meaningful weight — gradient boosting on labelled
    history usually outperforms Poisson/Elo on common 1X2 matchups.
    """
    if xgboost_available:
        return 0.45, 0.20, 0.35
    base = config.poisson_weight + config.elo_weight
    return config.poisson_weight / base, config.elo_weight / base, 0.0


def _blend_probabilities(
    poisson_probs: OutcomeProbabilities,
    elo_probs: OutcomeProbabilities,
    xgb_probs: OutcomeProbabilities | None,
    market_probs: ImpliedProbabilities | None,
    fixture: FixtureFeatures,
    market_odds: MarketOdds | None,
    config: DecisionConfig,
    calibration_alpha: float = 1.0,
    calibration_draw_boost: float = 1.0,
) -> OutcomeProbabilities:
    poisson_share, elo_share, xgb_share = _model_shares(config, xgb_probs is not None)

    team_reliability = _team_reliability(fixture)
    market_reliability = _market_reliability(market_odds)
    market_weight = 0.0
    if market_probs is not None:
        market_weight = _adaptive_market_weight(team_reliability, market_reliability, config.market_anchor_weight)

    model_floor = 0.10 if team_reliability >= 0.25 else 0.05
    model_mass = max(model_floor, 1.0 - market_weight)

    # 1. Composite MODELE (Poisson + Elo + XGBoost), normalise a 1.
    model_home = (
        poisson_probs.home * poisson_share
        + elo_probs.home * elo_share
        + (xgb_probs.home * xgb_share if xgb_probs else 0.0)
    )
    model_draw = (
        poisson_probs.draw * poisson_share
        + elo_probs.draw * elo_share
        + (xgb_probs.draw * xgb_share if xgb_probs else 0.0)
    )
    model_away = (
        poisson_probs.away * poisson_share
        + elo_probs.away * elo_share
        + (xgb_probs.away * xgb_share if xgb_probs else 0.0)
    )
    model_total = model_home + model_draw + model_away
    if model_total > 0:
        model_home, model_draw, model_away = (
            model_home / model_total, model_draw / model_total, model_away / model_total
        )

    # 2. Calibration (temperature) sur la composante modele UNIQUEMENT :
    #    le backtest a montre la sous-confiance du bloc analytique ; l'ancre
    #    marche est deja calibree et ne doit pas etre re-affutee.
    model_home, model_draw, model_away = sharpen(
        model_home, model_draw, model_away, calibration_alpha, calibration_draw_boost
    )

    # 3. Melange final modele calibre + marche.
    home = model_mass * model_home + (market_probs.home * market_weight if market_probs else 0.0)
    draw = model_mass * model_draw + (market_probs.draw * market_weight if market_probs else 0.0)
    away = model_mass * model_away + (market_probs.away * market_weight if market_probs else 0.0)

    total = home + draw + away
    if total <= 0:
        return OutcomeProbabilities(home=1 / 3, draw=1 / 3, away=1 / 3)
    return OutcomeProbabilities(home=home / total, draw=draw / total, away=away / total)


def _estimate_confidence(
    poisson_probs: OutcomeProbabilities,
    elo_output: EloOutput,
    blended: OutcomeProbabilities,
    fixture: FixtureFeatures,
    market_odds: MarketOdds | None,
) -> float:
    disagreement = (
        abs(poisson_probs.home - elo_output.probabilities.home)
        + abs(poisson_probs.draw - elo_output.probabilities.draw)
        + abs(poisson_probs.away - elo_output.probabilities.away)
    ) / 3.0
    dominance = max(blended.home, blended.draw, blended.away)
    runner_up = sorted((blended.home, blended.draw, blended.away), reverse=True)[1]
    dominance_gap = max(0.0, dominance - runner_up)
    gap_signal = min(1.0, abs(elo_output.rating_gap) / 250.0)
    team_reliability = _team_reliability(fixture)
    market_reliability = _market_reliability(market_odds)

    knowledge_signal = max(team_reliability, market_reliability * 0.85)
    epistemic_floor = 0.18 + 0.12 * knowledge_signal

    confidence = (
        epistemic_floor
        + dominance_gap * 0.32
        + dominance * 0.10
        + gap_signal * 0.10
        + team_reliability * 0.20
        + market_reliability * 0.18
        - disagreement * 0.18
    )
    return max(0.0, min(1.0, confidence))


class MatchAnalysisEngine:
    def __init__(
        self,
        config: EngineConfig | None = None,
        xgboost_predictor: XGBoostLike | None = None,
        calibration_alpha: float | None = None,
    ) -> None:
        self._config = config or EngineConfig()
        self._xgboost = xgboost_predictor
        # None = charger (alpha, draw_boost) depuis models/calibration.json
        # ((1.0, 1.0) si absent). Passer explicitement calibration_alpha=1.0
        # desactive toute la calibration (necessaire a l'ajustement lui-meme,
        # cf. run_calibration_fit).
        if calibration_alpha is not None:
            self._calibration_alpha = calibration_alpha
            self._calibration_draw_boost = 1.0
        else:
            self._calibration_alpha, self._calibration_draw_boost = load_calibration()

    def analyze_fixture(
        self,
        fixture: FixtureFeatures,
        market_odds: MarketOdds | None = None,
    ) -> AnalysisResult:
        poisson_output = estimate_poisson_probabilities(fixture, self._config.poisson)
        elo_output = estimate_elo_probabilities(fixture, self._config.elo)
        implied_probabilities = remove_overround(market_odds) if market_odds else None

        xgb_probs: OutcomeProbabilities | None = None
        if self._xgboost is not None and self._xgboost.available:
            xgb_probs = self._xgboost.predict(fixture, market_odds)

        blended = _blend_probabilities(
            poisson_output.probabilities,
            elo_output.probabilities,
            xgb_probs,
            implied_probabilities,
            fixture,
            market_odds,
            self._config.decision,
            calibration_alpha=self._calibration_alpha,
            calibration_draw_boost=self._calibration_draw_boost,
        )
        confidence_score = _estimate_confidence(
            poisson_output.probabilities,
            elo_output,
            blended,
            fixture,
            market_odds,
        )
        selections = build_selection_decisions(
            probabilities=blended,
            market_odds=market_odds,
            implied_probabilities=implied_probabilities,
            confidence_score=confidence_score,
            config=self._config.decision,
            consensus_odds=market_odds,
        )

        team_reliability = _team_reliability(fixture)
        market_reliability = _market_reliability(market_odds)
        knowledge_signal = max(team_reliability, market_reliability * 0.85)
        verdict = build_verdict(blended, confidence_score, knowledge_signal)
        explanation = build_explanation(
            fixture,
            poisson_output,
            elo_output,
            implied_probabilities,
            blended,
            verdict,
            market_odds,
        )

        return AnalysisResult(
            fixture_id=fixture.fixture_id,
            expected_home_goals=poisson_output.expected_home_goals,
            expected_away_goals=poisson_output.expected_away_goals,
            probabilities=blended,
            poisson_probabilities=poisson_output.probabilities,
            elo_probabilities=elo_output.probabilities,
            implied_probabilities=implied_probabilities,
            selections=selections,
            top_selection=selections[0],
            verdict=verdict,
            explanation=explanation,
        )
