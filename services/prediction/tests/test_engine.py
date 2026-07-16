from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.domain import FixtureFeatures, MarketOdds, TeamStrengthSnapshot
from spe_prediction.engine import MatchAnalysisEngine


class MatchAnalysisEngineTest(unittest.TestCase):
    def test_engine_returns_probabilities_that_sum_to_one(self) -> None:
        engine = MatchAnalysisEngine()
        fixture = FixtureFeatures(
            fixture_id=1,
            home_team=TeamStrengthSnapshot(1, "Home", 1700, 0.35, 0.20),
            away_team=TeamStrengthSnapshot(2, "Away", 1650, 0.18, 0.25),
        )
        market = MarketOdds(bookmaker="Stake", home_odd=2.10, draw_odd=3.40, away_odd=3.70)

        result = engine.analyze_fixture(fixture, market)

        self.assertAlmostEqual(
            result.probabilities.home + result.probabilities.draw + result.probabilities.away,
            1.0,
            places=6,
        )
        self.assertGreater(result.expected_home_goals, 0.0)
        self.assertGreater(result.expected_away_goals, 0.0)
        self.assertEqual(len(result.selections), 3)

    def test_top_selection_is_sorted_first(self) -> None:
        engine = MatchAnalysisEngine()
        fixture = FixtureFeatures(
            fixture_id=2,
            home_team=TeamStrengthSnapshot(3, "A", 1680, 0.31, 0.20, recent_form=0.22),
            away_team=TeamStrengthSnapshot(4, "B", 1580, 0.15, 0.28, recent_form=-0.05),
        )
        market = MarketOdds(bookmaker="Stake", home_odd=2.50, draw_odd=3.20, away_odd=3.00)

        result = engine.analyze_fixture(fixture, market)

        self.assertEqual(result.top_selection, result.selections[0])
        self.assertGreaterEqual(result.selections[0].ranking_score, result.selections[-1].ranking_score)

    def test_market_anchor_differentiates_sparse_history_matches(self) -> None:
        engine = MatchAnalysisEngine()
        neutral_home = TeamStrengthSnapshot(10, "Home", 1500, 0.20, 0.20, played_matches=0, data_quality=0.0)
        neutral_away = TeamStrengthSnapshot(11, "Away", 1500, 0.20, 0.20, played_matches=0, data_quality=0.0)

        home_favorite = engine.analyze_fixture(
            FixtureFeatures(
                fixture_id=3,
                home_team=neutral_home,
                away_team=neutral_away,
                external_adjustments={"home_goal_delta": 0.35, "away_goal_delta": -0.35},
            ),
            MarketOdds(
                bookmaker="Consensus",
                home_odd=1.65,
                draw_odd=3.90,
                away_odd=5.30,
                source_count=20,
                home_spread=0.04,
                draw_spread=0.05,
                away_spread=0.08,
            ),
        )
        away_favorite = engine.analyze_fixture(
            FixtureFeatures(
                fixture_id=4,
                home_team=neutral_home,
                away_team=neutral_away,
                external_adjustments={"home_goal_delta": -0.40, "away_goal_delta": 0.40},
            ),
            MarketOdds(
                bookmaker="Consensus",
                home_odd=6.20,
                draw_odd=4.20,
                away_odd=1.52,
                source_count=20,
                home_spread=0.08,
                draw_spread=0.05,
                away_spread=0.03,
            ),
        )

        self.assertGreater(home_favorite.probabilities.home, away_favorite.probabilities.home)
        self.assertGreater(away_favorite.probabilities.away, home_favorite.probabilities.away)
        self.assertNotAlmostEqual(
            home_favorite.expected_home_goals,
            away_favorite.expected_home_goals,
            places=2,
        )


if __name__ == "__main__":
    unittest.main()
