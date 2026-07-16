from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.golf_settlement import (
    flat_profit,
    parse_position,
    settle_matchup,
    settle_outright,
)


class GolfSettlementTests(unittest.TestCase):
    def test_parse_position(self) -> None:
        self.assertEqual(parse_position("1"), (1, True))
        self.assertEqual(parse_position("T5"), (5, True))
        self.assertEqual(parse_position("T27"), (27, True))
        self.assertEqual(parse_position("CUT"), (None, False))
        self.assertEqual(parse_position("WD"), (None, False))
        self.assertEqual(parse_position(""), (None, False))

    def test_outright(self) -> None:
        self.assertEqual(settle_outright("TOURNAMENT_WINNER", 1, True), "WON")
        self.assertEqual(settle_outright("TOURNAMENT_WINNER", 2, True), "LOST")
        self.assertEqual(settle_outright("TOP_5", 5, True), "WON")
        self.assertEqual(settle_outright("TOP_5", 6, True), "LOST")
        self.assertEqual(settle_outright("TOP_20", 20, True), "WON")
        self.assertEqual(settle_outright("MAKE_CUT", None, True), "WON")
        self.assertEqual(settle_outright("MAKE_CUT", None, False), "LOST")
        # Joueur non classe (cut) perd tout sauf make_cut.
        self.assertEqual(settle_outright("TOP_10", None, False), "LOST")

    def test_matchup(self) -> None:
        self.assertEqual(settle_matchup(5, 12), "WON")   # pick finit mieux
        self.assertEqual(settle_matchup(12, 5), "LOST")
        self.assertEqual(settle_matchup(5, 5), "PUSH")   # egalite
        self.assertEqual(settle_matchup(3, None), "WON")  # adversaire cut
        self.assertEqual(settle_matchup(None, 3), "LOST")
        self.assertEqual(settle_matchup(None, None), "PUSH")

    def test_flat_profit(self) -> None:
        self.assertEqual(flat_profit("WON", 1.83), 0.83)
        self.assertEqual(flat_profit("LOST", 1.83), -1.0)
        self.assertEqual(flat_profit("PUSH", 1.83), 0.0)
        self.assertIsNone(flat_profit("WON", None))


if __name__ == "__main__":
    unittest.main()
