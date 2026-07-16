from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.golf_engine import (
    blend_probability,
    build_market_predictions,
    normalize_market,
)


class GolfEngineTests(unittest.TestCase):
    def test_blend(self) -> None:
        self.assertAlmostEqual(blend_probability(0.10, 0.20, 0.70), 0.17)
        # Degrade proprement sur le modele disponible.
        self.assertEqual(blend_probability(None, 0.20), 0.20)
        self.assertEqual(blend_probability(0.10, None), 0.10)
        self.assertIsNone(blend_probability(None, None))

    def test_normalize_winner_sums_to_one(self) -> None:
        probs = {1: 0.30, 2: 0.20, 3: 0.10}  # somme 0.6 -> scale x(1/0.6)
        out = normalize_market(probs, "TOURNAMENT_WINNER")
        self.assertAlmostEqual(sum(out.values()), 1.0, places=6)
        self.assertAlmostEqual(out[1], 0.5, places=6)

    def test_normalize_topn_sums_to_n(self) -> None:
        probs = {i: 0.5 for i in range(1, 25)}  # somme 12 -> cible 10
        out = normalize_market(probs, "TOP_10")
        self.assertAlmostEqual(sum(out.values()), 10.0, places=4)

    def test_make_cut_not_normalized(self) -> None:
        probs = {1: 0.9, 2: 0.4}
        self.assertEqual(normalize_market(probs, "MAKE_CUT"), probs)

    def test_build_market_predictions(self) -> None:
        rows = [
            {"dg_id": 1, "player_id": 11, "selection_name": "A",
             "prob_baseline": 0.10, "prob_fit": 0.20},
            {"dg_id": 2, "player_id": 12, "selection_name": "B",
             "prob_baseline": 0.30, "prob_fit": None},  # fit absent -> baseline
            {"dg_id": 3, "player_id": None, "selection_name": "C",
             "prob_baseline": None, "prob_fit": None},  # ignore
        ]
        preds = build_market_predictions(rows, "TOURNAMENT_WINNER", fit_weight=0.70)
        self.assertEqual(len(preds), 2)
        self.assertAlmostEqual(sum(p.probability for p in preds), 1.0, places=4)
        # B (0.30) > A blend (0.17) -> B premier apres normalisation.
        self.assertEqual(preds[0].selection_name, "B")
        self.assertIsNotNone(preds[0].fair_odd)


if __name__ == "__main__":
    unittest.main()
