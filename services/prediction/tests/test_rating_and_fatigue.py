"""Tests for opponent-adjusted Elo (rating.py), calendar fatigue and the
prediction explanation generator."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.domain import FixtureFeatures, MarketOdds, TeamStrengthSnapshot
from spe_prediction.engine import MatchAnalysisEngine
from spe_prediction.rating import (
    BASE_RATING,
    compute_elo_ratings,
    competition_weight,
    is_neutral_venue,
)
from spe_prediction.repository import PostgresPredictionRepository


NOW = datetime(2026, 7, 2, tzinfo=timezone.utc)


def _match(days_ago: int, home: int, away: int, hs: int, aws: int, league: str = "FIFA World Cup Qualification"):
    return (NOW - timedelta(days=days_ago), home, away, hs, aws, league)


class OpponentAdjustedEloTest(unittest.TestCase):
    def test_beating_strong_opponent_earns_more_than_beating_weak(self) -> None:
        # Team 10 builds a strong rating by winning repeatedly.
        history = [
            _match(300 - i * 10, 10, 100 + i, 3, 0) for i in range(10)
        ]
        # Team 1 beats the strong team 10; team 2 beats a fresh unknown team 200.
        history.append(_match(50, 1, 10, 2, 0))
        history.append(_match(50, 2, 200, 2, 0))
        ratings = compute_elo_ratings(history)
        gain_vs_strong = ratings[1] - BASE_RATING
        gain_vs_weak = ratings[2] - BASE_RATING
        self.assertGreater(gain_vs_strong, gain_vs_weak)

    def test_ratings_are_zero_sum_per_match(self) -> None:
        ratings = compute_elo_ratings([_match(10, 1, 2, 1, 0)])
        self.assertAlmostEqual(
            (ratings[1] - BASE_RATING) + (ratings[2] - BASE_RATING), 0.0, places=9
        )

    def test_bigger_margin_moves_ratings_more(self) -> None:
        small = compute_elo_ratings([_match(10, 1, 2, 1, 0)])
        large = compute_elo_ratings([_match(10, 1, 2, 5, 0)])
        self.assertGreater(large[1], small[1])

    def test_world_cup_match_moves_ratings_more_than_friendly(self) -> None:
        wc = compute_elo_ratings([_match(10, 1, 2, 2, 0, "FIFA World Cup")])
        friendly = compute_elo_ratings([_match(10, 1, 2, 2, 0, "International Friendly Games")])
        self.assertGreater(wc[1], friendly[1])

    def test_neutral_venue_detection(self) -> None:
        self.assertTrue(is_neutral_venue("FIFA World Cup"))
        self.assertFalse(is_neutral_venue("FIFA World Cup Qualification"))
        self.assertFalse(is_neutral_venue("English Premier League"))

    def test_competition_weight_canonical_hierarchy(self) -> None:
        self.assertGreater(competition_weight("FIFA World Cup"), competition_weight("UEFA Nations League"))
        self.assertLess(competition_weight("International Friendly Games"), 1.0)


class FatiguePenaltyTest(unittest.TestCase):
    def test_no_history_means_no_fatigue(self) -> None:
        self.assertEqual(
            PostgresPredictionRepository.compute_fatigue_penalty(None, 0, NOW), 0.0
        )

    def test_short_rest_produces_high_fatigue(self) -> None:
        two_days_ago = NOW - timedelta(days=2)
        fatigue = PostgresPredictionRepository.compute_fatigue_penalty(two_days_ago, 5, NOW)
        self.assertGreater(fatigue, 0.60)

    def test_long_rest_produces_low_fatigue(self) -> None:
        two_weeks_ago = NOW - timedelta(days=14)
        fatigue = PostgresPredictionRepository.compute_fatigue_penalty(two_weeks_ago, 1, NOW)
        self.assertLess(fatigue, 0.10)

    def test_congestion_alone_adds_fatigue(self) -> None:
        week_ago = NOW - timedelta(days=7)
        calm = PostgresPredictionRepository.compute_fatigue_penalty(week_ago, 1, NOW)
        congested = PostgresPredictionRepository.compute_fatigue_penalty(week_ago, 6, NOW)
        self.assertGreater(congested, calm + 0.20)

    def test_fatigue_shifts_engine_probabilities(self) -> None:
        engine = MatchAnalysisEngine()
        fresh = TeamStrengthSnapshot(1, "Fresh", 1600, 0.30, 0.25, played_matches=20, data_quality=0.7)
        tired = TeamStrengthSnapshot(
            1, "Tired", 1600, 0.30, 0.25, fatigue_penalty=0.9, played_matches=20, data_quality=0.7
        )
        opponent = TeamStrengthSnapshot(2, "Opp", 1600, 0.30, 0.25, played_matches=20, data_quality=0.7)

        with_fresh = engine.analyze_fixture(FixtureFeatures(1, fresh, opponent))
        with_tired = engine.analyze_fixture(FixtureFeatures(2, tired, opponent))
        self.assertLess(with_tired.probabilities.home, with_fresh.probabilities.home)


class ExplanationTest(unittest.TestCase):
    def test_explanation_names_predicted_outcome_and_market_view(self) -> None:
        engine = MatchAnalysisEngine()
        fixture = FixtureFeatures(
            fixture_id=1,
            home_team=TeamStrengthSnapshot(1, "France", 1820, 0.45, 0.20, recent_form=0.3, played_matches=30, data_quality=0.8),
            away_team=TeamStrengthSnapshot(2, "Suede", 1590, 0.20, 0.30, recent_form=-0.1, played_matches=30, data_quality=0.8),
        )
        market = MarketOdds(bookmaker="C", home_odd=1.48, draw_odd=4.6, away_odd=6.2, source_count=15, home_spread=0.04, draw_spread=0.04, away_spread=0.04)
        result = engine.analyze_fixture(fixture, market)

        self.assertTrue(result.explanation)
        self.assertIn("France", result.explanation)
        self.assertIn("Pronostic", result.explanation)
        self.assertIn("marche", result.explanation)

    def test_explanation_flags_low_data_quality(self) -> None:
        engine = MatchAnalysisEngine()
        fixture = FixtureFeatures(
            fixture_id=2,
            home_team=TeamStrengthSnapshot(1, "Inconnu FC", 1500, 0.20, 1.20, played_matches=0, data_quality=0.0),
            away_team=TeamStrengthSnapshot(2, "Argentine", 1500, 0.20, 1.20, played_matches=0, data_quality=0.0),
        )
        market = MarketOdds(bookmaker="C", home_odd=20.0, draw_odd=9.0, away_odd=1.12, source_count=15, home_spread=0.04, draw_spread=0.04, away_spread=0.04)
        result = engine.analyze_fixture(fixture, market)
        self.assertIn("historique", result.explanation.lower())


if __name__ == "__main__":
    unittest.main()
