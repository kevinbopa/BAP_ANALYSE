from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.golf import GolfMarketOdd, consensus_predictions, golf_deal_candidates


class GolfModelTests(unittest.TestCase):
    def test_consensus_devigs_each_bookmaker_market(self) -> None:
        odds = [
            GolfMarketOdd(1, 10, 1, "TOURNAMENT_WINNER", "Player A", 2.0),
            GolfMarketOdd(1, 11, 1, "TOURNAMENT_WINNER", "Player B", 3.0),
            GolfMarketOdd(1, 12, 1, "TOURNAMENT_WINNER", "Player C", 6.0),
        ]

        predictions = consensus_predictions(odds)
        total = sum(pred.probability for pred in predictions)

        self.assertAlmostEqual(total, 1.0, places=6)
        self.assertEqual(predictions[0].selection_name, "Player A")
        self.assertGreater(predictions[0].probability, predictions[1].probability)
        self.assertGreater(predictions[0].confidence, 0)
        self.assertGreaterEqual(predictions[0].market_depth, 0)

    def test_deal_candidates_require_positive_edge(self) -> None:
        consensus = [
            GolfMarketOdd(1, 10, 1, "TOURNAMENT_WINNER", "Player A", 2.2),
            GolfMarketOdd(1, 11, 1, "TOURNAMENT_WINNER", "Player B", 2.2),
            GolfMarketOdd(1, 10, 2, "TOURNAMENT_WINNER", "Player A", 2.3),
            GolfMarketOdd(1, 11, 2, "TOURNAMENT_WINNER", "Player B", 2.1),
        ]
        predictions = consensus_predictions(consensus)
        target_odds = [
            GolfMarketOdd(1, 10, 3, "TOURNAMENT_WINNER", "Player A", 3.0),
            GolfMarketOdd(1, 11, 3, "TOURNAMENT_WINNER", "Player B", 1.5),
        ]

        candidates = golf_deal_candidates(predictions, target_odds, min_edge=0.03)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["selection_name"], "Player A")
        self.assertGreater(candidates[0]["edge_probability"], 0)
        self.assertIn("confidence", candidates[0])


if __name__ == "__main__":
    unittest.main()
