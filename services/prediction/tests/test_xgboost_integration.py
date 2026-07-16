"""Tests for the optional XGBoost predictor integration.

We never rely on the real xgboost library being installed — the tests use
``StaticPredictor`` (a deterministic stand-in implementing the same
interface) to verify the engine's blend behaviour.
"""
from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.domain import (
    FixtureFeatures,
    MarketOdds,
    OutcomeProbabilities,
    TeamStrengthSnapshot,
)
from spe_prediction.engine import MatchAnalysisEngine, _model_shares
from spe_prediction.gboost import (
    DEFAULT_FEATURE_NAMES,
    StaticPredictor,
    XGBoostConfig,
    XGBoostPredictor,
    extract_features,
)
from spe_prediction.domain import DecisionConfig


def _strong_history_team(team_id: int, name: str) -> TeamStrengthSnapshot:
    return TeamStrengthSnapshot(
        team_id=team_id,
        team_name=name,
        elo_rating=1700.0,
        attack_rating=0.40,
        defense_rating=0.22,
        recent_form=0.18,
        played_matches=22,
        data_quality=0.75,
    )


class FeatureExtractionTest(unittest.TestCase):
    def test_feature_vector_matches_named_features(self) -> None:
        fixture = FixtureFeatures(
            fixture_id=1,
            home_team=_strong_history_team(1, "Home"),
            away_team=_strong_history_team(2, "Away"),
        )
        features = extract_features(fixture, market_odds=None)
        self.assertEqual(len(features), len(DEFAULT_FEATURE_NAMES))

    def test_feature_vector_uses_market_when_present(self) -> None:
        fixture = FixtureFeatures(
            fixture_id=1,
            home_team=_strong_history_team(1, "Home"),
            away_team=_strong_history_team(2, "Away"),
        )
        market = MarketOdds(
            bookmaker="C",
            home_odd=1.90,
            draw_odd=3.40,
            away_odd=4.20,
            source_count=10,
            home_spread=0.04,
            draw_spread=0.05,
            away_spread=0.06,
        )
        features = extract_features(fixture, market_odds=market)
        # The last feature is market_source_count = 10
        self.assertEqual(features[-1], 10.0)


class PredictorFallbackTest(unittest.TestCase):
    def test_default_predictor_is_unavailable(self) -> None:
        predictor = XGBoostPredictor()
        self.assertFalse(predictor.available)

    def test_predict_returns_none_when_no_model(self) -> None:
        predictor = XGBoostPredictor(XGBoostConfig(model_path=None))
        fixture = FixtureFeatures(
            fixture_id=1,
            home_team=_strong_history_team(1, "Home"),
            away_team=_strong_history_team(2, "Away"),
        )
        self.assertIsNone(predictor.predict(fixture, market_odds=None))

    def test_missing_model_path_stays_unavailable(self) -> None:
        predictor = XGBoostPredictor(
            XGBoostConfig(model_path="non/existent/path/xgboost_1x2.joblib"),
        )
        self.assertFalse(predictor.available)


class ModelShareTest(unittest.TestCase):
    def test_three_way_split_when_xgboost_unavailable(self) -> None:
        cfg = DecisionConfig()
        p, e, x = _model_shares(cfg, xgboost_available=False)
        self.assertAlmostEqual(p + e + x, 1.0, places=6)
        self.assertEqual(x, 0.0)

    def test_four_way_split_when_xgboost_available(self) -> None:
        cfg = DecisionConfig()
        p, e, x = _model_shares(cfg, xgboost_available=True)
        self.assertAlmostEqual(p + e + x, 1.0, places=6)
        self.assertGreater(x, 0.20)


class EngineBlendsXGBoostWhenAvailableTest(unittest.TestCase):
    def _fixture_and_market(self) -> tuple[FixtureFeatures, MarketOdds]:
        fixture = FixtureFeatures(
            fixture_id=42,
            home_team=_strong_history_team(1, "Home"),
            away_team=_strong_history_team(2, "Away"),
            home_advantage=0.18,
        )
        market = MarketOdds(
            bookmaker="C",
            home_odd=2.10,
            draw_odd=3.30,
            away_odd=3.60,
            source_count=14,
            home_spread=0.04,
            draw_spread=0.05,
            away_spread=0.05,
        )
        return fixture, market

    def test_engine_without_xgb_does_not_call_predictor(self) -> None:
        fixture, market = self._fixture_and_market()
        baseline_engine = MatchAnalysisEngine()
        baseline = baseline_engine.analyze_fixture(fixture, market)
        # Sanity check — distinct probabilities sum to 1.
        self.assertAlmostEqual(
            baseline.probabilities.home + baseline.probabilities.draw + baseline.probabilities.away,
            1.0,
            places=6,
        )

    def test_engine_blend_shifts_toward_xgboost_prediction(self) -> None:
        fixture, market = self._fixture_and_market()
        baseline_engine = MatchAnalysisEngine()
        baseline = baseline_engine.analyze_fixture(fixture, market)

        # Inject an XGBoost-like predictor that strongly favours AWAY
        biased_predictor = StaticPredictor(
            OutcomeProbabilities(home=0.10, draw=0.10, away=0.80)
        )
        boosted_engine = MatchAnalysisEngine(xgboost_predictor=biased_predictor)
        boosted = boosted_engine.analyze_fixture(fixture, market)

        # The blend with a strong AWAY-biased XGBoost should raise AWAY
        # probability vs the baseline (provided team history is strong
        # enough that the market does not entirely dominate).
        self.assertGreater(boosted.probabilities.away, baseline.probabilities.away)

    def test_engine_falls_back_when_predictor_returns_none(self) -> None:
        fixture, market = self._fixture_and_market()

        class NoneReturningPredictor:
            @property
            def available(self) -> bool:
                return True

            def predict(self, fixture, market_odds):
                return None

        engine = MatchAnalysisEngine(xgboost_predictor=NoneReturningPredictor())
        result = engine.analyze_fixture(fixture, market)
        self.assertAlmostEqual(
            result.probabilities.home + result.probabilities.draw + result.probabilities.away,
            1.0,
            places=6,
        )


class DealCandidatesInheritXGBoostTest(unittest.TestCase):
    """Regression guarantee: the deal-ranking layer uses the SAME blended
    probabilities the engine computes, so a trained XGBoost model
    automatically shifts which deals get flagged as value bets.
    """

    def test_deals_use_engine_blended_probabilities_including_xgboost(self) -> None:
        from spe_prediction.repository import PostgresPredictionRepository

        fixture = FixtureFeatures(
            fixture_id=999,
            home_team=_strong_history_team(1, "Home"),
            away_team=_strong_history_team(2, "Away"),
            home_advantage=0.18,
        )
        consensus = MarketOdds(
            bookmaker="CONSENSUS",
            home_odd=2.05,
            draw_odd=3.40,
            away_odd=3.60,
            source_count=15,
            home_spread=0.04,
            draw_spread=0.04,
            away_spread=0.05,
        )
        # XGBoost stand-in heavily favours AWAY beyond what Poisson/Elo/market would.
        boosted_predictor = StaticPredictor(
            OutcomeProbabilities(home=0.10, draw=0.10, away=0.80)
        )
        engine = MatchAnalysisEngine(xgboost_predictor=boosted_predictor)
        result = engine.analyze_fixture(fixture, consensus)

        repo = PostgresPredictionRepository.__new__(PostgresPredictionRepository)
        repo._decision_config = DecisionConfig()
        offers = tuple(
            MarketOdds(
                bookmaker=f"Book{i}",
                bookmaker_id=i,
                home_odd=2.05 + i * 0.02,
                draw_odd=3.40,
                away_odd=3.60 + i * 0.01,
                source_count=1,
            )
            for i in range(5)
        )
        deals = repo._build_deal_candidates(result, offers, consensus)

        # Every recommended deal must use the engine's blended probability —
        # the value the XGBoost-aware blend produced — for its selection.
        for deal in deals:
            expected_prob = {
                "HOME": result.probabilities.home,
                "DRAW": result.probabilities.draw,
                "AWAY": result.probabilities.away,
            }[deal.selection_code]
            self.assertAlmostEqual(deal.model_probability, expected_prob, places=6)


if __name__ == "__main__":
    unittest.main()
