"""Tests for the player-layer signals (availability) and the extra-sportive
context foundation."""
from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.context import aggregate_context_factors, merge_adjustments
from spe_prediction.domain import FixtureFeatures, TeamStrengthSnapshot
from spe_prediction.engine import MatchAnalysisEngine
from spe_prediction.player_signals import (
    DEFAULT_PLAYER_WEIGHT,
    compute_weighted_availability,
)


class AvailabilityTest(unittest.TestCase):
    def test_full_squad_is_neutral(self) -> None:
        squad = [(f"P{i}", 6.8, True) for i in range(23)]
        result = compute_weighted_availability(squad)
        self.assertEqual(result.availability_index, 1.0)
        self.assertEqual(result.key_absences, ())

    def test_empty_squad_is_neutral(self) -> None:
        result = compute_weighted_availability([])
        self.assertEqual(result.availability_index, 1.0)

    def test_losing_star_hurts_more_than_bench_player(self) -> None:
        base = [(f"P{i}", 6.5, True) for i in range(20)]
        with_star_out = base + [("Star", 8.2, False), ("Bench", 6.0, True)]
        with_bench_out = base + [("Star", 8.2, True), ("Bench", 6.0, False)]
        star_out = compute_weighted_availability(with_star_out)
        bench_out = compute_weighted_availability(with_bench_out)
        self.assertLess(star_out.availability_index, bench_out.availability_index)
        self.assertIn("Star", star_out.key_absences)
        self.assertEqual(bench_out.key_absences, ())

    def test_unknown_rating_falls_back_to_default_weight(self) -> None:
        squad = [("A", None, True), ("B", None, False)]
        result = compute_weighted_availability(squad)
        self.assertAlmostEqual(result.availability_index, 0.5, places=6)
        self.assertEqual(result.injured_count, 1)
        # DEFAULT_PLAYER_WEIGHT < KEY_PLAYER_RATING → pas d'absence "cle"
        self.assertEqual(result.key_absences, ())
        self.assertGreater(DEFAULT_PLAYER_WEIGHT, 0)

    def test_engine_shifts_probabilities_when_squad_is_depleted(self) -> None:
        engine = MatchAnalysisEngine()
        full = TeamStrengthSnapshot(1, "Full", 1650, 0.30, 0.25, played_matches=20, data_quality=0.7)
        depleted = TeamStrengthSnapshot(
            1, "Depleted", 1650, 0.30, 0.25, played_matches=20, data_quality=0.7,
            availability_index=0.80, key_absences=("Star Striker",),
        )
        opponent = TeamStrengthSnapshot(2, "Opp", 1650, 0.30, 0.25, played_matches=20, data_quality=0.7)

        with_full = engine.analyze_fixture(FixtureFeatures(1, full, opponent))
        with_depleted = engine.analyze_fixture(FixtureFeatures(2, depleted, opponent))
        self.assertLess(with_depleted.probabilities.home, with_full.probabilities.home)

    def test_explanation_names_key_absences(self) -> None:
        engine = MatchAnalysisEngine()
        depleted = TeamStrengthSnapshot(
            1, "France", 1800, 0.40, 0.22, played_matches=25, data_quality=0.8,
            availability_index=0.82, key_absences=("Mbappe",),
        )
        opponent = TeamStrengthSnapshot(2, "Suede", 1600, 0.20, 0.30, played_matches=25, data_quality=0.8)
        result = engine.analyze_fixture(FixtureFeatures(3, depleted, opponent))
        self.assertIn("Mbappe", result.explanation)
        self.assertIn("Effectif reduit", result.explanation)


class ContextFactorsTest(unittest.TestCase):
    def test_zero_sum_factor_moves_teams_in_opposite_directions(self) -> None:
        deltas = aggregate_context_factors([("STAKES", 1.0, 1.0)])
        self.assertGreater(deltas["home_goal_delta"], 0)
        self.assertLess(deltas["away_goal_delta"], 0)
        self.assertAlmostEqual(deltas["home_goal_delta"], -deltas["away_goal_delta"], places=6)

    def test_weather_depresses_both_teams(self) -> None:
        deltas = aggregate_context_factors([("WEATHER", -1.0, 1.0)])
        self.assertLess(deltas["home_goal_delta"], 0)
        self.assertLess(deltas["away_goal_delta"], 0)

    def test_factor_swing_is_capped(self) -> None:
        deltas = aggregate_context_factors([("STAKES", 5.0, 5.0)])  # valeurs hors bornes
        self.assertLessEqual(abs(deltas["home_goal_delta"]), 0.15)

    def test_unknown_factor_is_ignored(self) -> None:
        deltas = aggregate_context_factors([("ALIEN_INVASION", 1.0, 1.0)])
        self.assertEqual(deltas["home_goal_delta"], 0.0)

    def test_weight_scales_the_effect(self) -> None:
        full = aggregate_context_factors([("TRAVEL", 1.0, 1.0)])
        half = aggregate_context_factors([("TRAVEL", 1.0, 0.5)])
        self.assertAlmostEqual(half["home_goal_delta"], full["home_goal_delta"] / 2, places=6)

    def test_merge_adds_to_existing_adjustments(self) -> None:
        base = {"home_goal_delta": 0.20, "away_goal_delta": -0.10}
        extra = {"home_goal_delta": 0.05, "away_goal_delta": -0.05}
        merged = merge_adjustments(base, extra)
        self.assertAlmostEqual(merged["home_goal_delta"], 0.25, places=6)
        self.assertAlmostEqual(merged["away_goal_delta"], -0.15, places=6)


if __name__ == "__main__":
    unittest.main()
