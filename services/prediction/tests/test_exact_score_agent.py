from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.domain import FixtureFeatures, OutcomeProbabilities, TeamStrengthSnapshot
from spe_prediction.exact_score import ExactScoreAgent, build_exact_score_distribution


class ExactScoreAgentTest(unittest.TestCase):
    def test_exact_score_distribution_sums_to_one(self) -> None:
        agent = ExactScoreAgent()
        fixture = FixtureFeatures(
            fixture_id=91,
            home_team=TeamStrengthSnapshot(1, "Home", 1710, 0.40, 0.22, recent_form=0.30),
            away_team=TeamStrengthSnapshot(2, "Away", 1625, 0.18, 0.30, recent_form=0.02),
            home_advantage=0.18,
        )

        result = agent.analyze_fixture(fixture)

        self.assertAlmostEqual(
            sum(row.probability for row in result.full_distribution),
            1.0,
            places=6,
        )
        self.assertEqual(len(result.top_scores), 5)
        self.assertGreaterEqual(result.top_scores[0].probability, result.top_scores[-1].probability)

    def test_exact_score_buckets_match_main_engine_probabilities(self) -> None:
        agent = ExactScoreAgent(top_n=7)
        fixture = FixtureFeatures(
            fixture_id=92,
            home_team=TeamStrengthSnapshot(3, "A", 1685, 0.33, 0.23, recent_form=0.18),
            away_team=TeamStrengthSnapshot(4, "B", 1600, 0.17, 0.29, recent_form=-0.04),
            home_advantage=0.14,
        )

        result = agent.analyze_fixture(fixture)

        home_total = sum(row.probability for row in result.full_distribution if row.outcome_code == "HOME")
        draw_total = sum(row.probability for row in result.full_distribution if row.outcome_code == "DRAW")
        away_total = sum(row.probability for row in result.full_distribution if row.outcome_code == "AWAY")

        self.assertAlmostEqual(home_total, result.match_analysis.probabilities.home, places=6)
        self.assertAlmostEqual(draw_total, result.match_analysis.probabilities.draw, places=6)
        self.assertAlmostEqual(away_total, result.match_analysis.probabilities.away, places=6)

    def test_home_favorite_has_home_leaning_top_score(self) -> None:
        agent = ExactScoreAgent(top_n=3)
        fixture = FixtureFeatures(
            fixture_id=93,
            home_team=TeamStrengthSnapshot(5, "Fav", 1760, 0.48, 0.20, recent_form=0.32),
            away_team=TeamStrengthSnapshot(6, "Dog", 1560, 0.14, 0.31, recent_form=-0.08),
            home_advantage=0.20,
        )

        result = agent.analyze_fixture(fixture)

        self.assertGreaterEqual(result.top_scores[0].home_goals, result.top_scores[0].away_goals)
        self.assertGreater(result.top_scores[0].fair_odd, 1.0)

    def test_availability_shapes_high_score_tails(self) -> None:
        target = OutcomeProbabilities(home=0.55, draw=0.24, away=0.21)
        full = FixtureFeatures(
            fixture_id=101,
            home_team=TeamStrengthSnapshot(1, "Home", 1700, 0.30, 0.24, availability_index=1.0),
            away_team=TeamStrengthSnapshot(2, "Away", 1600, 0.22, 0.29, availability_index=1.0),
        )
        depleted = FixtureFeatures(
            fixture_id=102,
            home_team=TeamStrengthSnapshot(1, "Home", 1700, 0.30, 0.24, availability_index=0.76),
            away_team=TeamStrengthSnapshot(2, "Away", 1600, 0.22, 0.29, availability_index=1.0),
        )

        _top_full, full_distribution = build_exact_score_distribution(1.85, 1.10, target, fixture=full)
        _top_dep, dep_distribution = build_exact_score_distribution(1.85, 1.10, target, fixture=depleted)

        full_high_tail = sum(row.probability for row in full_distribution if row.home_goals >= 3)
        dep_high_tail = sum(row.probability for row in dep_distribution if row.home_goals >= 3)
        self.assertLess(dep_high_tail, full_high_tail)

    def test_btts_insight_boosts_both_teams_scoring_rows(self) -> None:
        target = OutcomeProbabilities(home=0.40, draw=0.28, away=0.32)
        plain = FixtureFeatures(
            fixture_id=103,
            home_team=TeamStrengthSnapshot(1, "Home", 1670, 0.28, 0.25),
            away_team=TeamStrengthSnapshot(2, "Away", 1650, 0.26, 0.26),
        )
        btts = FixtureFeatures(
            fixture_id=104,
            home_team=TeamStrengthSnapshot(1, "Home", 1670, 0.28, 0.25),
            away_team=TeamStrengthSnapshot(2, "Away", 1650, 0.26, 0.26),
            insights=("Profil: les deux equipes marquent (BTTS) = 63% (moyenne globale 48%)",),
        )

        _plain_top, plain_distribution = build_exact_score_distribution(1.35, 1.25, target, fixture=plain)
        _btts_top, btts_distribution = build_exact_score_distribution(1.35, 1.25, target, fixture=btts)

        plain_btts_mass = sum(
            row.probability for row in plain_distribution if row.home_goals > 0 and row.away_goals > 0
        )
        btts_mass = sum(
            row.probability for row in btts_distribution if row.home_goals > 0 and row.away_goals > 0
        )
        self.assertGreater(btts_mass, plain_btts_mass)


if __name__ == "__main__":
    unittest.main()
