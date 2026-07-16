from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.outrights import (
    RemainingMatch,
    ScorerState,
    outright_deal_candidates,
    performance_index,
    simulate_knockout,
    simulate_league_title,
    top_scorer_probabilities,
)


class LeagueTitleSimulationTest(unittest.TestCase):
    def test_dominant_leader_wins_almost_always(self) -> None:
        # Equipe 1 : 20 pts d'avance, 2 matchs restants -> titre quasi acquis.
        remaining = [
            RemainingMatch(1, 2, 0.40, 0.30, 0.30),
            RemainingMatch(2, 1, 0.40, 0.30, 0.30),
        ]
        titles = simulate_league_title(remaining, {1: 60.0, 2: 40.0}, simulations=2000)
        self.assertGreater(titles.get(1, 0.0), 0.99)

    def test_probabilities_sum_to_one(self) -> None:
        remaining = [
            RemainingMatch(1, 2, 0.45, 0.28, 0.27),
            RemainingMatch(3, 1, 0.35, 0.30, 0.35),
            RemainingMatch(2, 3, 0.42, 0.28, 0.30),
        ]
        titles = simulate_league_title(remaining, {1: 10, 2: 10, 3: 10}, simulations=3000)
        self.assertAlmostEqual(sum(titles.values()), 1.0, places=9)

    def test_stronger_team_more_likely_champion(self) -> None:
        # Equipe 1 favorite de chaque match : elle doit dominer les titres.
        remaining = [
            RemainingMatch(1, 2, 0.60, 0.22, 0.18),
            RemainingMatch(2, 1, 0.18, 0.22, 0.60),
            RemainingMatch(1, 3, 0.60, 0.22, 0.18),
            RemainingMatch(3, 2, 0.40, 0.28, 0.32),
        ]
        titles = simulate_league_title(remaining, {}, simulations=4000)
        self.assertGreater(titles.get(1, 0.0), titles.get(2, 0.0))
        self.assertGreater(titles.get(1, 0.0), titles.get(3, 0.0))

    def test_empty_inputs(self) -> None:
        self.assertEqual(simulate_league_title([], {}, simulations=100), {})


class KnockoutSimulationTest(unittest.TestCase):
    def test_single_final(self) -> None:
        final = [RemainingMatch(1, 2, 0.60, 0.20, 0.20)]
        wins = simulate_knockout([final], simulations=5000)
        # Nuls repartis au prorata : p1 = 0.6/0.8 = 0.75.
        self.assertAlmostEqual(wins.get(1, 0.0), 0.75, delta=0.03)
        self.assertAlmostEqual(sum(wins.values()), 1.0, places=9)

    def test_bracket_with_future_rounds(self) -> None:
        quarter_finals = [
            RemainingMatch(1, 2, 0.70, 0.15, 0.15),
            RemainingMatch(3, 4, 0.50, 0.20, 0.30),
        ]
        # Equipe 1 tres forte en confrontation future aussi.
        def strong_one(a, b):
            return 0.8 if a == 1 else (0.2 if b == 1 else 0.5)
        wins = simulate_knockout([quarter_finals], simulations=5000, win_probability_fn=strong_one)
        self.assertGreater(wins.get(1, 0.0), 0.5)
        self.assertAlmostEqual(sum(wins.values()), 1.0, places=9)

    def test_empty(self) -> None:
        self.assertEqual(simulate_knockout([], simulations=100), {})


class TopScorerTest(unittest.TestCase):
    def test_leader_with_no_remaining_matches_holds(self) -> None:
        scorers = [
            ScorerState(1, "Leader", 10, goals=15, matches_played=15),
            ScorerState(2, "Poursuivant", 20, goals=10, matches_played=15),
        ]
        # Aucun match restant : le leader garde son avance a 100%.
        probs = top_scorer_probabilities(scorers, {10: 0, 20: 0}, simulations=2000)
        self.assertAlmostEqual(probs.get(1, 0.0), 1.0, places=9)

    def test_hot_scorer_with_many_remaining_can_overtake(self) -> None:
        scorers = [
            ScorerState(1, "Leader", 10, goals=12, matches_played=15),
            ScorerState(2, "Serial", 20, goals=11, matches_played=10),  # 1.1/match
        ]
        probs = top_scorer_probabilities(scorers, {10: 5, 20: 12}, simulations=4000)
        self.assertGreater(probs.get(2, 0.0), probs.get(1, 0.0))

    def test_probabilities_normalized(self) -> None:
        scorers = [
            ScorerState(i, f"J{i}", 10 * i, goals=8 + i, matches_played=14)
            for i in range(1, 5)
        ]
        probs = top_scorer_probabilities(scorers, {10: 8, 20: 8, 30: 8, 40: 8}, simulations=3000)
        self.assertAlmostEqual(sum(probs.values()), 1.0, places=6)


class PerformanceIndexTest(unittest.TestCase):
    def test_ranking_order_and_normalization(self) -> None:
        rows = [
            (1, "Star", 40.0, 15.0, 1.1),
            (2, "Bon", 25.0, 10.0, 1.0),
            (3, "Moyen", 12.0, 5.0, 1.0),
        ]
        ranking = performance_index(rows)
        self.assertEqual(ranking[0]["player_name"], "Star")
        self.assertEqual(ranking[0]["index"], 1.0)
        self.assertGreater(ranking[1]["index"], ranking[2]["index"])

    def test_empty(self) -> None:
        self.assertEqual(performance_index([]), [])


class OutrightDealTest(unittest.TestCase):
    def test_underpriced_candidate_is_a_deal(self) -> None:
        # Modele 30%, marche (normalise) ~20% -> edge 10 pts.
        model = {1: 0.30, 2: 0.25, 3: 0.45}
        odds = {1: 5.0, 2: 4.0, 3: 1.8}
        candidates = outright_deal_candidates(model, odds)
        self.assertTrue(any(c["selection_id"] == 1 for c in candidates))
        best = candidates[0]
        self.assertGreaterEqual(best["edge_probability"], 0.03)

    def test_fairly_priced_market_has_no_deal(self) -> None:
        # Cotes alignees sur le modele (avec marge) : aucun edge suffisant.
        model = {1: 0.50, 2: 0.30, 3: 0.20}
        odds = {1: 1.90, 2: 3.15, 3: 4.75}
        self.assertEqual(outright_deal_candidates(model, odds), [])

    def test_huge_edge_rejected(self) -> None:
        # Edge de 40 pts = erreur de modele quasi certaine a cet horizon.
        model = {1: 0.60, 2: 0.40}
        odds = {1: 5.0, 2: 1.25}
        candidates = outright_deal_candidates(model, odds)
        self.assertFalse(any(c["selection_id"] == 1 for c in candidates))


if __name__ == "__main__":
    unittest.main()
