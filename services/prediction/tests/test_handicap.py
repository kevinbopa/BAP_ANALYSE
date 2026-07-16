from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.derived_markets import handicap_win_probability
from spe_prediction.domain import ExactScorePrediction
from spe_prediction.settlement import settle_handicap


def _dist(rows):
    return tuple(
        ExactScorePrediction(home_goals=h, away_goals=a, probability=p,
                             fair_odd=1.0 / p, outcome_code="X")
        for h, a, p in rows
    )


class HandicapSettlementTests(unittest.TestCase):
    def _profit(self, margin, line, is_home, odd=2.0):
        return settle_handicap(margin, line, is_home, odd)[1]

    def test_integer_line_push(self) -> None:
        # home -1, gagne de 1 -> push (VOID)
        self.assertEqual(settle_handicap(1, -1.0, True, 2.0), ("VOID", 0.0))
        # home -1, gagne de 2 -> WON plein (cote 2.0 -> +1)
        self.assertEqual(settle_handicap(2, -1.0, True, 2.0), ("WON", 1.0))
        # home -1, nul -> LOST
        self.assertEqual(settle_handicap(0, -1.0, True, 2.0), ("LOST", -1.0))

    def test_half_line_no_push(self) -> None:
        # home -0.5, gagne de 1 -> WON ; nul -> LOST
        self.assertEqual(settle_handicap(1, -0.5, True, 2.0), ("WON", 1.0))
        self.assertEqual(settle_handicap(0, -0.5, True, 2.0), ("LOST", -1.0))

    def test_quarter_line_half_win_loss(self) -> None:
        # home -0.75, gagne de 1 -> demi-gain (moitie sur -0.5 gagne, moitie sur -1 push)
        code, profit = settle_handicap(1, -0.75, True, 2.0)
        self.assertEqual(code, "WON")
        self.assertAlmostEqual(profit, 0.5)  # (2-1)/2
        # home -1.25, gagne de 1 -> demi-perte (moitie -1 push, moitie -1.5 perd)
        code, profit = settle_handicap(1, -1.25, True, 2.0)
        self.assertEqual(code, "LOST")
        self.assertAlmostEqual(profit, -0.5)

    def test_away_side(self) -> None:
        # away +0.5, match nul (margin home 0) -> away gagne
        self.assertEqual(settle_handicap(0, 0.5, False, 2.0), ("WON", 1.0))
        # away +0.5, home gagne de 1 -> away perd
        self.assertEqual(settle_handicap(1, 0.5, False, 2.0), ("LOST", -1.0))
        # away -1 (favori exterieur), gagne de 2 a l'exterieur (margin home -2) -> WON
        self.assertEqual(settle_handicap(-2, -1.0, False, 2.0), ("WON", 1.0))


class HandicapProbabilityTests(unittest.TestCase):
    def test_probability_from_margin_distribution(self) -> None:
        # 50% victoire 2-0 (marge +2), 30% 1-1 (0), 20% 0-1 (-1).
        dist = _dist([(2, 0, 0.5), (1, 1, 0.3), (0, 1, 0.2)])
        # home -1.5 : couvre seulement si marge > 1.5 -> 0.5
        self.assertAlmostEqual(handicap_win_probability(dist, -1.5, True), 0.5)
        # home -1 : marge 2 gagne (0.5) ; marge 0/-1 perdent ; pas de push -> 0.5
        self.assertAlmostEqual(handicap_win_probability(dist, -1.0, True), 0.5)
        # home -2 : marge 2 = push (0.5 * 0.5) -> 0.25
        self.assertAlmostEqual(handicap_win_probability(dist, -2.0, True), 0.25)
        # away +2 : miroir -> 1 - 0.25 = 0.75
        self.assertAlmostEqual(handicap_win_probability(dist, 2.0, False), 0.75)
        # complementarite home@L + away@-L = 1
        for line in (-1.5, -1.0, -0.5, 0.5, 1.25):
            total = (handicap_win_probability(dist, line, True)
                     + handicap_win_probability(dist, -line, False))
            self.assertAlmostEqual(total, 1.0, places=9)


if __name__ == "__main__":
    unittest.main()
