"""Tests for the pure-prediction layer (verdict + surety)."""
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
from spe_prediction.engine import MatchAnalysisEngine
from spe_prediction.forecaster import build_verdict, compute_surety, draw_is_competitive


class ComputeSuretyTest(unittest.TestCase):
    def test_dominant_favorite_yields_high_surety(self) -> None:
        surety = compute_surety(top_probability=0.80, margin=0.65, confidence_score=0.70, knowledge_signal=0.70)
        self.assertGreater(surety, 0.75)

    def test_coin_flip_yields_low_surety_even_with_confidence(self) -> None:
        # Three-way 35/35/30: top 0.35, margin 0.0, confidence high.
        surety = compute_surety(top_probability=0.35, margin=0.0, confidence_score=0.80, knowledge_signal=0.70)
        self.assertLess(surety, 0.45)

    def test_surety_stays_in_unit_interval(self) -> None:
        for top, margin, conf, know in [
            (0.0, 0.0, 0.0, 0.0),
            (1.0, 1.0, 1.0, 1.0),
            (0.5, 0.5, 0.5, 0.5),
        ]:
            self.assertGreaterEqual(compute_surety(top, margin, conf, know), 0.0)
            self.assertLessEqual(compute_surety(top, margin, conf, know), 1.0)


class BuildVerdictTest(unittest.TestCase):
    def test_picks_max_probability_outcome(self) -> None:
        probs = OutcomeProbabilities(home=0.18, draw=0.22, away=0.60)
        verdict = build_verdict(probs, confidence_score=0.70, knowledge_signal=0.60)
        self.assertEqual(verdict.selection_code, "AWAY")
        self.assertAlmostEqual(verdict.probability, 0.60, places=6)
        self.assertAlmostEqual(verdict.runner_up_probability, 0.22, places=6)
        self.assertAlmostEqual(verdict.margin, 0.38, places=6)

    def test_label_is_human_readable_french(self) -> None:
        probs = OutcomeProbabilities(home=0.55, draw=0.25, away=0.20)
        verdict = build_verdict(probs, confidence_score=0.65)
        self.assertEqual(verdict.label, "Victoire domicile")
        self.assertIn("Victoire domicile", verdict.rationale)


class DrawCompetitiveTest(unittest.TestCase):
    def test_tight_match_flags_draw(self) -> None:
        # 36/31/33 : le nul talonne a 5 pts avec 31% -> competitif.
        probs = OutcomeProbabilities(home=0.36, draw=0.31, away=0.33)
        verdict = build_verdict(probs, confidence_score=0.70)
        self.assertEqual(verdict.selection_code, "HOME")  # argmax intact
        self.assertTrue(verdict.draw_competitive)
        self.assertIn("Nul competitif", verdict.rationale)

    def test_clear_favorite_does_not_flag_draw(self) -> None:
        # 55/25/20 : nul a 30 pts du max -> pas competitif.
        probs = OutcomeProbabilities(home=0.55, draw=0.25, away=0.20)
        verdict = build_verdict(probs, confidence_score=0.70)
        self.assertFalse(verdict.draw_competitive)

    def test_low_draw_probability_not_flagged_even_if_close(self) -> None:
        # 30/26/44... nul a 26% : sous le plancher de 28% -> pas competitif.
        self.assertFalse(draw_is_competitive(0.30, 0.26, 0.44))

    def test_draw_as_top_pick_is_not_flagged(self) -> None:
        # Nul deja pronostic : le flag serait redondant.
        probs = OutcomeProbabilities(home=0.32, draw=0.36, away=0.32)
        verdict = build_verdict(probs, confidence_score=0.70)
        self.assertEqual(verdict.selection_code, "DRAW")
        self.assertFalse(verdict.draw_competitive)

    def test_boundary_conditions(self) -> None:
        self.assertTrue(draw_is_competitive(0.36, 0.28, 0.34))    # pile au plancher
        self.assertFalse(draw_is_competitive(0.40, 0.28, 0.30))   # ecart > 8 pts... 12 pts
        self.assertTrue(draw_is_competitive(0.37, 0.29, 0.34))


class EngineProducesVerdictTest(unittest.TestCase):
    def test_engine_attaches_verdict_consistent_with_blended_probs(self) -> None:
        engine = MatchAnalysisEngine()
        fixture = FixtureFeatures(
            fixture_id=2001,
            home_team=TeamStrengthSnapshot(1, "A", 1700, 0.35, 0.20, played_matches=20, data_quality=0.7),
            away_team=TeamStrengthSnapshot(2, "B", 1620, 0.18, 0.30, played_matches=20, data_quality=0.7),
        )
        market = MarketOdds(bookmaker="C", home_odd=1.95, draw_odd=3.50, away_odd=4.10, source_count=10, home_spread=0.05, draw_spread=0.05, away_spread=0.05)
        result = engine.analyze_fixture(fixture, market)

        # Verdict's selection must match the actual argmax of blended probs.
        probs_map = result.probabilities.as_dict()
        expected_code = max(probs_map, key=probs_map.get)
        self.assertEqual(result.verdict.selection_code, expected_code)
        self.assertAlmostEqual(result.verdict.probability, probs_map[expected_code], places=6)
        self.assertGreaterEqual(result.verdict.surety_score, 0.0)
        self.assertLessEqual(result.verdict.surety_score, 1.0)

    def test_clear_favorite_is_more_sure_than_coin_flip_engine_level(self) -> None:
        engine = MatchAnalysisEngine()
        anchor_team = TeamStrengthSnapshot(1, "T", 1500, 0.20, 0.20, played_matches=12, data_quality=0.5)
        coin_flip_market = MarketOdds(bookmaker="C", home_odd=2.85, draw_odd=3.10, away_odd=2.70, source_count=15, home_spread=0.04, draw_spread=0.04, away_spread=0.04)
        clear_favorite_market = MarketOdds(bookmaker="C", home_odd=1.18, draw_odd=7.0, away_odd=14.0, source_count=15, home_spread=0.04, draw_spread=0.04, away_spread=0.04)

        coin_flip = engine.analyze_fixture(
            FixtureFeatures(fixture_id=1, home_team=anchor_team, away_team=anchor_team, external_adjustments={"home_goal_delta": 0.02, "away_goal_delta": -0.02}),
            coin_flip_market,
        )
        clear_favorite = engine.analyze_fixture(
            FixtureFeatures(fixture_id=2, home_team=anchor_team, away_team=anchor_team, external_adjustments={"home_goal_delta": 0.45, "away_goal_delta": -0.45}),
            clear_favorite_market,
        )
        self.assertGreater(
            clear_favorite.verdict.surety_score - coin_flip.verdict.surety_score,
            0.20,
        )


if __name__ == "__main__":
    unittest.main()
