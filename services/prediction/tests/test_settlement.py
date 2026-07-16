from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.settlement import flat_profit, regulation_score, settle_result


class SettleResultTest(unittest.TestCase):
    def test_home_win(self) -> None:
        self.assertEqual(settle_result("HOME", 2, 0), "WON")
        self.assertEqual(settle_result("AWAY", 2, 0), "LOST")
        self.assertEqual(settle_result("DRAW", 2, 0), "LOST")

    def test_draw(self) -> None:
        self.assertEqual(settle_result("DRAW", 1, 1), "WON")
        self.assertEqual(settle_result("HOME", 1, 1), "LOST")

    def test_away_win(self) -> None:
        self.assertEqual(settle_result("AWAY", 0, 3), "WON")

    def test_over_under_25(self) -> None:
        self.assertEqual(settle_result("OVER", 2, 1), "WON")
        self.assertEqual(settle_result("OVER", 1, 1), "LOST")
        self.assertEqual(settle_result("UNDER", 1, 1), "WON")
        self.assertEqual(settle_result("UNDER", 0, 3), "LOST")
        # Frontiere : exactement 3 buts -> over gagne (ligne 2.5, pas de push)
        self.assertEqual(settle_result("OVER", 3, 0), "WON")
        self.assertEqual(settle_result("UNDER", 2, 0), "WON")

    def test_btts(self) -> None:
        self.assertEqual(settle_result("BTTS_YES", 1, 1), "WON")
        self.assertEqual(settle_result("BTTS_YES", 3, 0), "LOST")
        self.assertEqual(settle_result("BTTS_NO", 3, 0), "WON")
        self.assertEqual(settle_result("BTTS_NO", 1, 2), "LOST")
        self.assertEqual(settle_result("BTTS_NO", 0, 0), "WON")


class RegulationScoreTest(unittest.TestCase):
    def test_no_extra_time_returns_final(self) -> None:
        # Match tranche en 90 min : score final = reglementaire.
        self.assertEqual(regulation_score(2, 1, 2, 1, 2, 1, False), (2, 1))

    def test_extra_time_goal_reverts_to_90(self) -> None:
        # Final 2-1 mais 1-1 a la 90e, but vainqueur en prolongation.
        # Le pari 1X2 se regle sur le nul (90 min), pas sur la victoire.
        self.assertEqual(regulation_score(2, 1, 1, 1, 2, 1, True), (1, 1))

    def test_incoherent_timeline_falls_back_to_final(self) -> None:
        # Timeline incomplete (total != score officiel) : on ne devine pas.
        self.assertEqual(regulation_score(3, 1, 1, 1, 2, 1, True), (3, 1))

    def test_missing_totals_falls_back(self) -> None:
        self.assertEqual(regulation_score(2, 1, None, None, None, None, True), (2, 1))

    def test_settlement_uses_regulation_for_1x2(self) -> None:
        # Un DRAW mise sur un match 1-1 (90 min) puis 2-1 (prolongation) GAGNE.
        reg = regulation_score(2, 1, 1, 1, 2, 1, True)
        self.assertEqual(settle_result("DRAW", *reg), "WON")
        # Le HOME (parieur qui aurait cru a la victoire dans les 90 min) PERD.
        self.assertEqual(settle_result("HOME", *reg), "LOST")


class FlatProfitTest(unittest.TestCase):
    def test_won_pays_odd_minus_one(self) -> None:
        self.assertAlmostEqual(flat_profit("WON", 7.19), 6.19, places=4)

    def test_lost_costs_one_unit(self) -> None:
        self.assertEqual(flat_profit("LOST", 7.19), -1.0)

    def test_invalid_odd_is_void(self) -> None:
        self.assertIsNone(flat_profit("WON", None))
        self.assertIsNone(flat_profit("WON", 1.0))


if __name__ == "__main__":
    unittest.main()
