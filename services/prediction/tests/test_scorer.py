from __future__ import annotations

import math
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.scorer import (
    PlayerGoalRecord,
    position_factor,
    scorer_deal_candidates,
    scorer_probability,
    team_scorer_probabilities,
)


class PositionFactorTest(unittest.TestCase):
    def test_position_hierarchy(self) -> None:
        self.assertLess(position_factor("G"), position_factor("D"))
        self.assertLess(position_factor("D"), position_factor("M"))
        self.assertLessEqual(position_factor("M"), position_factor("F"))

    def test_unknown_and_midfield_neutral(self) -> None:
        self.assertEqual(position_factor(None), 1.0)
        self.assertEqual(position_factor("M"), 1.0)
        self.assertEqual(position_factor("XYZ"), 1.0)

    def test_position_dampens_defender_probability(self) -> None:
        # Meme part de buts, mais un defenseur est regularise a la baisse.
        forward = scorer_probability(1.8, 20, 100, position_code="F")
        defender = scorer_probability(1.8, 20, 100, position_code="D")
        keeper = scorer_probability(1.8, 20, 100, position_code="G")
        self.assertGreater(forward, defender)
        self.assertGreater(defender, keeper)


class ScorerProbabilityTest(unittest.TestCase):
    def test_share_based_poisson(self) -> None:
        # Equipe attendue a 1.8 but ; joueur = 30% des buts de l'equipe.
        # lambda = 1.8 * 0.30 = 0.54 ; P = 1 - exp(-0.54).
        p = scorer_probability(1.8, player_goals=30, team_total_goals=100)
        self.assertAlmostEqual(p, 1 - math.exp(-0.54), places=6)

    def test_more_attacking_match_raises_probability(self) -> None:
        low = scorer_probability(1.0, 20, 100)
        high = scorer_probability(2.5, 20, 100)
        self.assertGreater(high, low)

    def test_insufficient_sample_returns_zero(self) -> None:
        self.assertEqual(scorer_probability(1.8, player_goals=1, team_total_goals=100), 0.0)
        self.assertEqual(scorer_probability(1.8, player_goals=20, team_total_goals=5), 0.0)

    def test_probability_capped(self) -> None:
        p = scorer_probability(4.0, player_goals=90, team_total_goals=100)
        self.assertLessEqual(p, 0.90)


class RichSignalsTest(unittest.TestCase):
    def test_form_raises_and_lowers_probability(self) -> None:
        base = scorer_probability(1.8, 30, 100, form_factor=1.0)
        hot = scorer_probability(1.8, 30, 100, form_factor=1.4)
        cold = scorer_probability(1.8, 30, 100, form_factor=0.7)
        self.assertGreater(hot, base)
        self.assertLess(cold, base)

    def test_availability_penalizes_absent_player(self) -> None:
        present = scorer_probability(1.8, 30, 100, availability=1.0)
        benched = scorer_probability(1.8, 30, 100, availability=0.2)
        self.assertLess(benched, present)
        # Un joueur qui n'a pas joue recemment voit sa proba s'effondrer.
        self.assertLess(benched, present * 0.5)

    def test_form_and_availability_clamped(self) -> None:
        # Signaux extremes bornes : pas d'explosion de lambda.
        huge = scorer_probability(1.8, 30, 100, form_factor=99.0, availability=99.0)
        capped = scorer_probability(1.8, 30, 100, form_factor=1.45, availability=1.0)
        self.assertAlmostEqual(huge, capped, places=6)

    def test_recency_weighted_share_used(self) -> None:
        # Meme buts bruts, mais poids recence different -> proba differente.
        recent = scorer_probability(1.8, 20, 100, weighted_player_goals=18,
                                    weighted_team_goals=50)
        old = scorer_probability(1.8, 20, 100, weighted_player_goals=6,
                                 weighted_team_goals=50)
        self.assertGreater(recent, old)


class TeamScorerTest(unittest.TestCase):
    def test_only_meaningful_probabilities_kept(self) -> None:
        players = [
            PlayerGoalRecord(1, "Star", goals=35, matches=30),
            PlayerGoalRecord(2, "Bench", goals=2, matches=20),
        ]
        probs = team_scorer_probabilities(1.8, players, team_total_goals=100)
        self.assertIn(1, probs)  # star au-dessus du plancher
        # Bench: 1.8 * 0.02 = 0.036 -> P ~ 3.5% < plancher 5% -> exclu.
        self.assertNotIn(2, probs)


class ScorerDealTest(unittest.TestCase):
    def test_value_scorer_is_a_deal(self) -> None:
        # Modele 52%, cote 2.20 (implicite 45.5%) -> edge ~6.5 pts.
        deals = scorer_deal_candidates({1: 0.52}, {1: 2.20})
        self.assertEqual(len(deals), 1)
        self.assertAlmostEqual(deals[0]["edge_probability"], 0.52 - 1 / 2.20, places=4)

    def test_longshot_failsafe_rejects_elite_at_long_odds(self) -> None:
        # Modele 30% mais cote 13.0 (implicite 7.7%) -> edge 22 pts sur un
        # longshot : le book sait quelque chose (repos/blessure), on rejette.
        self.assertEqual(scorer_deal_candidates({1: 0.30}, {1: 13.0}), [])

    def test_huge_edge_hits_ceiling(self) -> None:
        # Edge 25 pts >= plafond 18% -> rejete.
        self.assertEqual(scorer_deal_candidates({1: 0.60}, {1: 2.90}), [])

    def test_no_edge_no_deal(self) -> None:
        # Modele aligne sur le marche.
        self.assertEqual(scorer_deal_candidates({1: 0.45}, {1: 2.20}), [])


if __name__ == "__main__":
    unittest.main()
