from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.derived_markets import (
    TwoWayOffer,
    binary_sharpen,
    build_two_way_decisions,
    consensus_two_way,
    derived_market_probabilities,
    load_derived_calibration,
)
from spe_prediction.domain import DecisionConfig, ExactScorePrediction


def _score(home: int, away: int, probability: float) -> ExactScorePrediction:
    outcome = "HOME" if home > away else "AWAY" if away > home else "DRAW"
    return ExactScorePrediction(
        home_goals=home,
        away_goals=away,
        probability=probability,
        fair_odd=1.0 / probability if probability > 0 else 999.0,
        outcome_code=outcome,
    )


class DerivedProbabilitiesTest(unittest.TestCase):
    def test_over_and_btts_sums(self) -> None:
        # Distribution jouet : 0-0 (20%), 1-0 (30%), 1-1 (25%), 2-1 (25%)
        distribution = (
            _score(0, 0, 0.20),
            _score(1, 0, 0.30),
            _score(1, 1, 0.25),
            _score(2, 1, 0.25),
        )
        probs = derived_market_probabilities(distribution)
        # Over 2.5 : seul 2-1 (3 buts) -> 25%
        self.assertAlmostEqual(probs["OVER"], 0.25, places=6)
        self.assertAlmostEqual(probs["UNDER"], 0.75, places=6)
        # BTTS oui : 1-1 et 2-1 -> 50%
        self.assertAlmostEqual(probs["BTTS_YES"], 0.50, places=6)
        self.assertAlmostEqual(probs["BTTS_NO"], 0.50, places=6)

    def test_unnormalized_distribution_is_renormalized(self) -> None:
        distribution = (_score(0, 0, 0.4), _score(2, 2, 0.4))  # somme 0.8
        probs = derived_market_probabilities(distribution)
        self.assertAlmostEqual(probs["OVER"], 0.5, places=6)
        self.assertAlmostEqual(probs["BTTS_YES"], 0.5, places=6)

    def test_empty_distribution_is_neutral(self) -> None:
        probs = derived_market_probabilities(())
        self.assertEqual(probs["OVER"], 0.5)
        self.assertEqual(probs["BTTS_YES"], 0.5)

    def test_complementarity(self) -> None:
        distribution = tuple(
            _score(h, a, 1.0 / 16.0) for h in range(4) for a in range(4)
        )
        probs = derived_market_probabilities(distribution)
        self.assertAlmostEqual(probs["OVER"] + probs["UNDER"], 1.0, places=9)
        self.assertAlmostEqual(probs["BTTS_YES"] + probs["BTTS_NO"], 1.0, places=9)


class TwoWayDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = DecisionConfig()

    def test_value_side_is_recommended(self) -> None:
        # Modele 62% over ; book price 50/50 (cotes 1.95/1.95, marge ~2.5%).
        probabilities = {"OVER": 0.62, "UNDER": 0.38, "BTTS_YES": 0.5, "BTTS_NO": 0.5}
        offer = TwoWayOffer("OU25", "Pinnacle", 1.95, 1.95, bookmaker_id=7)
        decisions = build_two_way_decisions(
            "OU25", probabilities, offer, confidence_score=0.75, config=self.config
        )
        self.assertEqual(decisions[0].selection_code, "OVER")
        self.assertTrue(decisions[0].recommended)
        self.assertFalse(decisions[1].recommended)
        self.assertEqual(decisions[0].market_code, "OU25")
        self.assertEqual(decisions[0].bookmaker_id, 7)

    def test_no_edge_no_recommendation(self) -> None:
        # Modele aligne sur le marche : aucune value.
        probabilities = {"OVER": 0.5, "UNDER": 0.5, "BTTS_YES": 0.5, "BTTS_NO": 0.5}
        offer = TwoWayOffer("OU25", "Pinnacle", 1.90, 1.90)
        decisions = build_two_way_decisions(
            "OU25", probabilities, offer, confidence_score=0.75, config=self.config
        )
        self.assertFalse(any(d.recommended for d in decisions))

    def test_huge_edge_hits_hard_ceiling(self) -> None:
        # Edge 25 pts >= plafond dur 18% -> faux edge, refuse.
        probabilities = {"OVER": 0.75, "UNDER": 0.25, "BTTS_YES": 0.5, "BTTS_NO": 0.5}
        offer = TwoWayOffer("OU25", "Pinnacle", 1.95, 1.95)
        decisions = build_two_way_decisions(
            "OU25", probabilities, offer, confidence_score=0.9, config=self.config
        )
        self.assertFalse(any(d.recommended for d in decisions))

    def test_longshot_failsafe(self) -> None:
        # Cote 12.0 (implied ~8%) avec edge 6 pts -> failsafe longshot.
        probabilities = {"BTTS_YES": 0.145, "BTTS_NO": 0.855, "OVER": 0.5, "UNDER": 0.5}
        offer = TwoWayOffer("BTTS", "Pinnacle", 12.0, 1.02 + 0.03)
        decisions = build_two_way_decisions(
            "BTTS", probabilities, offer, confidence_score=0.8, config=self.config
        )
        yes = next(d for d in decisions if d.selection_code == "BTTS_YES")
        self.assertFalse(yes.recommended)

    def test_both_sides_never_recommended_together(self) -> None:
        # Propriete structurelle : implied normalise somme a 1, donc les deux
        # edges somment a 0 — impossible d'avoir deux convictions.
        for p_over in (0.30, 0.45, 0.55, 0.70):
            probabilities = {
                "OVER": p_over, "UNDER": 1 - p_over, "BTTS_YES": 0.5, "BTTS_NO": 0.5,
            }
            offer = TwoWayOffer("OU25", "Pinnacle", 1.90, 2.00)
            decisions = build_two_way_decisions(
                "OU25", probabilities, offer, confidence_score=0.8, config=self.config
            )
            recommended = [d for d in decisions if d.recommended]
            self.assertLessEqual(len(recommended), 1)

    def test_consensus_median(self) -> None:
        offers = (
            TwoWayOffer("BTTS", "A", 1.80, 2.00),
            TwoWayOffer("BTTS", "B", 1.90, 1.90),
            TwoWayOffer("BTTS", "C", 2.00, 1.80),
        )
        consensus = consensus_two_way(offers)
        self.assertIsNotNone(consensus)
        self.assertEqual(consensus.bookmaker, "CONSENSUS")
        self.assertAlmostEqual(consensus.first_odd, 1.90)
        self.assertAlmostEqual(consensus.second_odd, 1.90)
        self.assertEqual(consensus.source_count, 3)
        self.assertIsNone(consensus_two_way(()))


class BestConvictionTest(unittest.TestCase):
    def _decision(self, market: str, code: str, score: float, book: str) -> "SelectionDecision":
        from spe_prediction.domain import SelectionDecision
        return SelectionDecision(
            selection_code=code,
            model_probability=0.6,
            fair_odd=1.67,
            market_odd=1.9,
            implied_probability=0.53,
            edge_probability=0.07,
            expected_value=0.14,
            fractional_kelly_fraction=0.02,
            confidence_score=0.8,
            ranking_score=score,
            recommended=True,
            rationale="test",
            bookmaker=book,
            market_code=market,
        )

    def test_single_conviction_across_markets(self) -> None:
        from spe_prediction.repository import PostgresPredictionRepository
        candidates = (
            self._decision("1X2", "HOME", 0.53, "Pinnacle"),
            self._decision("OU25", "OVER", 0.46, "Betsson"),
            self._decision("OU25", "OVER", 0.44, "William Hill"),
            self._decision("BTTS", "BTTS_YES", 0.43, "Pinnacle"),
        )
        kept = PostgresPredictionRepository._select_best_conviction(candidates)
        # Seule la conviction la mieux scoree (HOME) survit, tous marches confondus.
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].selection_code, "HOME")

    def test_two_books_max_on_winning_conviction(self) -> None:
        from spe_prediction.repository import PostgresPredictionRepository
        candidates = (
            self._decision("OU25", "OVER", 0.52, "Betsson"),
            self._decision("OU25", "OVER", 0.48, "William Hill"),
            self._decision("OU25", "OVER", 0.45, "GTbets"),
            self._decision("1X2", "HOME", 0.40, "Pinnacle"),
        )
        kept = PostgresPredictionRepository._select_best_conviction(candidates)
        self.assertEqual([d.bookmaker for d in kept], ["Betsson", "William Hill"])
        self.assertTrue(all(d.selection_code == "OVER" for d in kept))

    def test_empty_is_noop(self) -> None:
        from spe_prediction.repository import PostgresPredictionRepository
        self.assertEqual(PostgresPredictionRepository._select_best_conviction(()), ())


class BinaryCalibrationTest(unittest.TestCase):
    def test_identity_alpha_is_noop(self) -> None:
        for p in (0.1, 0.35, 0.5, 0.72, 0.9):
            self.assertAlmostEqual(binary_sharpen(p, 1.0), p, places=9)

    def test_fixed_points(self) -> None:
        # 0.5 reste 0.5 quel que soit alpha (symetrie).
        for alpha in (0.6, 1.3, 2.0):
            self.assertAlmostEqual(binary_sharpen(0.5, alpha), 0.5, places=9)

    def test_alpha_above_one_sharpens(self) -> None:
        self.assertGreater(binary_sharpen(0.7, 1.5), 0.7)
        self.assertLess(binary_sharpen(0.3, 1.5), 0.3)

    def test_alpha_below_one_flattens(self) -> None:
        self.assertLess(binary_sharpen(0.7, 0.7), 0.7)
        self.assertGreater(binary_sharpen(0.3, 0.7), 0.3)

    def test_complementarity_preserved(self) -> None:
        # sharpen(p) + sharpen(1-p) = 1 : over/under restent complementaires.
        for p in (0.2, 0.44, 0.61, 0.85):
            self.assertAlmostEqual(
                binary_sharpen(p, 1.4) + binary_sharpen(1.0 - p, 1.4), 1.0, places=9
            )

    def test_load_missing_file_returns_identity(self) -> None:
        load_derived_calibration.cache_clear()
        alphas = load_derived_calibration("Z:/nulle/part/calibration_derived.json")
        self.assertEqual(alphas, {"OU25": 1.0, "BTTS": 1.0})
        load_derived_calibration.cache_clear()

    def test_load_rejects_out_of_bounds_alpha(self) -> None:
        import json as json_module
        import tempfile
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            json_module.dump({"OU25": {"alpha": 9.0}, "BTTS": {"alpha": 1.2}}, handle)
            path = handle.name
        load_derived_calibration.cache_clear()
        alphas = load_derived_calibration(path)
        self.assertEqual(alphas["OU25"], 1.0)   # 9.0 hors bornes -> identite
        self.assertEqual(alphas["BTTS"], 1.2)
        load_derived_calibration.cache_clear()


if __name__ == "__main__":
    unittest.main()
