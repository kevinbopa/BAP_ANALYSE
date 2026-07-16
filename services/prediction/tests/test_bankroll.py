from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.bankroll import (
    RISK_PROFILES,
    build_parlay_suggestions,
    build_portfolio_plan,
    credible_probability,
    kelly_fraction,
    stake_fraction,
)


def _deal(model_p: float, implied_p: float, odd: float, label: str = "A vs B",
          confidence: float = 0.8) -> dict:
    return {
        "label": label,
        "market_code": "1X2",
        "selection_code": "HOME",
        "bookmaker": "Pinnacle",
        "kickoff_utc": None,
        "market_odd": odd,
        "model_probability": model_p,
        "implied_probability": implied_p,
        "confidence_score": confidence,
    }


class KellyTest(unittest.TestCase):
    def test_no_edge_no_stake(self) -> None:
        # p = 1/cote : EV nul -> Kelly nul.
        self.assertEqual(kelly_fraction(0.5, 2.0), 0.0)

    def test_negative_edge_no_stake(self) -> None:
        self.assertEqual(kelly_fraction(0.4, 2.0), 0.0)

    def test_known_value(self) -> None:
        # p=0.55, cote 2.0 : Kelly = (1*0.55 - 0.45)/1 = 0.10.
        self.assertAlmostEqual(kelly_fraction(0.55, 2.0), 0.10, places=9)

    def test_invalid_odd(self) -> None:
        self.assertEqual(kelly_fraction(0.9, 1.0), 0.0)


class CredibleProbabilityTest(unittest.TestCase):
    def test_full_credibility_keeps_model(self) -> None:
        self.assertAlmostEqual(credible_probability(0.60, 0.50, 1.0), 0.60)

    def test_zero_credibility_keeps_market(self) -> None:
        self.assertAlmostEqual(credible_probability(0.60, 0.50, 0.0), 0.50)

    def test_partial_shrinkage(self) -> None:
        self.assertAlmostEqual(credible_probability(0.60, 0.50, 0.5), 0.55)


class StakeFractionTest(unittest.TestCase):
    def test_cap_respected(self) -> None:
        profile = RISK_PROFILES["equilibre"]
        # Edge modere et credible -> mise > 0 mais <= plafond.
        stake = stake_fraction(0.56, 0.50, 2.00, 0.9, profile)
        self.assertGreater(stake, 0.0)
        self.assertLessEqual(stake, profile.stake_cap)

    def test_suspicious_edge_stakes_less_than_credible_edge(self) -> None:
        profile = RISK_PROFILES["equilibre"]
        # Edge 20 pts >= plafond dur -> credibilite 0 -> proba projetee = marche -> mise 0.
        huge = stake_fraction(0.70, 0.50, 2.00, 0.9, profile)
        sane = stake_fraction(0.55, 0.50, 2.00, 0.9, profile)
        self.assertEqual(huge, 0.0)
        self.assertGreater(sane, 0.0)

    def test_profiles_ordering(self) -> None:
        args = (0.56, 0.50, 2.00, 0.9)
        prudent = stake_fraction(*args, RISK_PROFILES["prudent"])
        equilibre = stake_fraction(*args, RISK_PROFILES["equilibre"])
        agressif = stake_fraction(*args, RISK_PROFILES["agressif"])
        self.assertLess(prudent, equilibre)
        self.assertLess(equilibre, agressif)


class PortfolioPlanTest(unittest.TestCase):
    def test_exposure_cap_scales_down(self) -> None:
        # 12 deals juteux : la somme des Kelly depasse le plafond -> scaling.
        deals = [_deal(0.57, 0.50, 2.05, label=f"M{i}") for i in range(12)]
        plan = build_portfolio_plan(deals, bankroll=1000, days=30,
                                    profile_code="equilibre", simulations=200)
        self.assertLessEqual(plan["exposure_pct"], 20.0 + 1e-6)
        self.assertEqual(len(plan["lines"]), 12)

    def test_zero_stake_positions_are_dropped(self) -> None:
        # Un pari sans edge apres credibilisation (Kelly ~ 0) ne doit PAS
        # apparaitre comme une position recommandee a 0$.
        no_edge = _deal(0.50, 0.50, 2.00, label="Sans edge")
        real = _deal(0.58, 0.50, 2.00, label="Avec edge")
        plan = build_portfolio_plan([no_edge, real], 1000, 30, simulations=100)
        labels = [line["label"] for line in plan["lines"]]
        self.assertIn("Avec edge", labels)
        self.assertNotIn("Sans edge", labels)
        # Aucune mise a 0$ dans le plan final.
        self.assertTrue(all(line["stake_amount"] > 0 for line in plan["lines"]))

    def test_win_profit_and_expected_profit_coherent(self) -> None:
        deals = [_deal(0.56, 0.50, 3.65)]
        plan = build_portfolio_plan(deals, 1000, 30, simulations=100)
        line = plan["lines"][0]
        # Gain si gagne = mise x (cote - 1).
        self.assertAlmostEqual(
            line["win_profit"], round(line["stake_amount"] * 2.65, 2), places=2
        )
        # Esperance = p x gain_si_gagne - (1-p) x mise.
        p = line["credible_probability"]
        self.assertAlmostEqual(
            line["expected_profit"],
            round(p * line["win_profit"] - (1 - p) * line["stake_amount"], 2),
            places=2,
        )
        # L'esperance est toujours inferieure au gain potentiel.
        self.assertLess(line["expected_profit"], line["win_profit"])

    def test_amounts_proportional_to_bankroll(self) -> None:
        deals = [_deal(0.56, 0.50, 2.00)]
        small = build_portfolio_plan(deals, 100, 30, simulations=200)
        big = build_portfolio_plan(deals, 10_000, 30, simulations=200)
        self.assertAlmostEqual(
            big["lines"][0]["stake_pct"],
            small["lines"][0]["stake_pct"],
            places=2,
        )

    def test_empty_board_still_projects(self) -> None:
        plan = build_portfolio_plan([], bankroll=500, days=30,
                                    bets_per_day=0.5, simulations=500)
        self.assertEqual(plan["lines"], [])
        # 15 paris futurs "typiques" simules quand meme.
        self.assertEqual(plan["simulation"]["expected_bets"], 15)
        self.assertGreater(plan["simulation"]["final_median"], 0)

    def test_simulation_deterministic_with_seed(self) -> None:
        deals = [_deal(0.56, 0.50, 2.00)]
        a = build_portfolio_plan(deals, 1000, 30, simulations=500, seed=7)
        b = build_portfolio_plan(deals, 1000, 30, simulations=500, seed=7)
        self.assertEqual(a["simulation"], b["simulation"])

    def test_simulation_percentiles_ordered(self) -> None:
        deals = [_deal(0.56, 0.50, 2.00, label=f"M{i}") for i in range(5)]
        plan = build_portfolio_plan(deals, 1000, 60, bets_per_day=0.4, simulations=2000)
        sim = plan["simulation"]
        self.assertLessEqual(sim["final_p5"], sim["final_median"])
        self.assertLessEqual(sim["final_median"], sim["final_p95"])
        self.assertGreaterEqual(sim["prob_loss_pct"], 0.0)
        self.assertLessEqual(sim["prob_loss_pct"], 100.0)

    def test_prudent_has_smaller_drawdown_risk_than_agressif(self) -> None:
        deals = [_deal(0.56, 0.50, 2.00, label=f"M{i}") for i in range(8)]
        prudent = build_portfolio_plan(deals, 1000, 90, bets_per_day=0.5,
                                       profile_code="prudent", simulations=3000)
        agressif = build_portfolio_plan(deals, 1000, 90, bets_per_day=0.5,
                                        profile_code="agressif", simulations=3000)
        self.assertLessEqual(
            prudent["simulation"]["prob_drawdown30_pct"],
            agressif["simulation"]["prob_drawdown30_pct"],
        )

    def test_profiles_select_different_books_of_risk(self) -> None:
        deals = [
            _deal(0.57, 0.50, 2.05, label="Edge propre"),
            _deal(0.45, 0.36, 3.20, label="Cote limite"),
            _deal(0.34, 0.24, 5.80, label="Long shot"),
            _deal(0.62, 0.57, 1.75, label="Favori serre"),
        ]
        prudent = build_portfolio_plan(deals, 1000, 30, profile_code="prudent", simulations=200)
        agressif = build_portfolio_plan(deals, 1000, 30, profile_code="agressif", simulations=200)
        prudent_labels = {line["label"] for line in prudent["lines"]}
        agressif_labels = {line["label"] for line in agressif["lines"]}
        self.assertNotIn("Long shot", prudent_labels)
        self.assertIn("Long shot", agressif_labels)
        self.assertLess(prudent["exposure_pct"], agressif["exposure_pct"])

    def test_category_caps_are_respected(self) -> None:
        deals = [
            _deal(0.38, 0.25, 4.50, label=f"Risque {i}", confidence=0.7)
            for i in range(8)
        ]
        plan = build_portfolio_plan(deals, 1000, 30, profile_code="equilibre", simulations=200)
        risky_exposure = sum(
            line["stake_fraction"] for line in plan["lines"] if line["category"] == "RISQUE"
        )
        self.assertLessEqual(risky_exposure, RISK_PROFILES["equilibre"].category_caps["RISQUE"] + 1e-9)

    def test_unknown_profile_falls_back(self) -> None:
        plan = build_portfolio_plan([], 1000, 30, profile_code="yolo", simulations=100)
        self.assertEqual(plan["profile"]["code"], "equilibre")


class ParlayTest(unittest.TestCase):
    def _line(self, label: str, p: float, odd: float, stake: float = 0.02) -> dict:
        return {
            "label": label, "selection_code": "HOME", "bookmaker": "Pinnacle",
            "market_odd": odd, "credible_probability": p, "stake_fraction": stake,
        }

    def test_two_value_legs_make_a_ticket(self) -> None:
        # Deux jambes 62%/60% a cote 1.9 : combi p=37%, cote 3.61, EV +34% >
        # la meilleure jambe seule (+18%).
        lines = [
            self._line("Match A", 0.62, 1.90),
            self._line("Match B", 0.60, 1.90),
        ]
        tickets = build_parlay_suggestions(lines, 1000, RISK_PROFILES["equilibre"])
        self.assertGreaterEqual(len(tickets), 1)
        best = tickets[0]
        self.assertEqual(len(best["legs"]), 2)
        self.assertGreater(best["expected_value"], 0.18)
        self.assertLessEqual(best["stake_pct"], 1.0)  # plafond 1% bankroll

    def test_prudent_has_no_parlays(self) -> None:
        lines = [
            self._line("Match A", 0.66, 1.90),
            self._line("Match B", 0.64, 1.90),
        ]
        self.assertEqual(build_parlay_suggestions(lines, 1000, RISK_PROFILES["prudent"]), [])

    def test_same_match_never_combined(self) -> None:
        lines = [
            self._line("Match A", 0.62, 1.90),
            self._line("Match A", 0.60, 1.95),  # meme match, autre pari
        ]
        self.assertEqual(build_parlay_suggestions(lines, 1000, RISK_PROFILES["equilibre"]), [])

    def test_low_probability_combo_rejected(self) -> None:
        # 45% x 45% = 20% < plancher 25% : trop improbable pour un ticket.
        lines = [
            self._line("Match A", 0.45, 2.60),
            self._line("Match B", 0.45, 2.60),
        ]
        self.assertEqual(build_parlay_suggestions(lines, 1000, RISK_PROFILES["equilibre"]), [])

    def test_no_edge_legs_make_no_ticket(self) -> None:
        # Jambes sans value (p x cote < 1) : le combine ne cree pas de valeur.
        lines = [
            self._line("Match A", 0.50, 1.90),
            self._line("Match B", 0.50, 1.90),
        ]
        self.assertEqual(build_parlay_suggestions(lines, 1000, RISK_PROFILES["equilibre"]), [])


if __name__ == "__main__":
    unittest.main()
