from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.decision import (
    EDGE_HARD_CEILING,
    _edge_credibility,
    build_selection_decisions,
)
from spe_prediction.domain import (
    DecisionConfig,
    FixtureFeatures,
    ImpliedProbabilities,
    MarketOdds,
    OutcomeProbabilities,
    TeamStrengthSnapshot,
)
from spe_prediction.engine import (
    MatchAnalysisEngine,
    _adaptive_market_weight,
)
from spe_prediction.repository import FixtureContext, PostgresPredictionRepository


def _no_history_team(team_id: int, name: str) -> TeamStrengthSnapshot:
    return TeamStrengthSnapshot(
        team_id=team_id,
        team_name=name,
        elo_rating=1500.0,
        attack_rating=0.20,
        defense_rating=1.20,
        recent_form=0.0,
        played_matches=0,
        data_quality=0.0,
    )


class AdaptiveMarketWeightTest(unittest.TestCase):
    def test_market_dominates_when_team_history_is_empty(self) -> None:
        weight = _adaptive_market_weight(team_reliability=0.0, market_reliability=0.8, base_anchor=0.15)
        self.assertGreaterEqual(weight, 0.55)

    def test_base_anchor_holds_when_team_history_is_strong(self) -> None:
        weight = _adaptive_market_weight(team_reliability=1.0, market_reliability=0.8, base_anchor=0.15)
        self.assertAlmostEqual(weight, 0.15, places=4)

    def test_no_market_yields_zero_weight(self) -> None:
        weight = _adaptive_market_weight(team_reliability=0.0, market_reliability=0.0, base_anchor=0.15)
        self.assertEqual(weight, 0.0)


class WorldCupHomogeneityTest(unittest.TestCase):
    """When team history is empty the blended probability should track the market,
    not the noisy independent Poisson default."""

    def _build_market(self, home: float, draw: float, away: float, count: int = 18) -> MarketOdds:
        return MarketOdds(
            bookmaker="CONSENSUS",
            home_odd=home,
            draw_odd=draw,
            away_odd=away,
            source_count=count,
            home_spread=0.05,
            draw_spread=0.05,
            away_spread=0.05,
        )

    def test_underdog_no_longer_inflates_to_double_digits(self) -> None:
        engine = MatchAnalysisEngine()
        # Argentina vs Jordan-type fixture: market gives away team ~95%, home ~4%.
        fixture = FixtureFeatures(
            fixture_id=101,
            home_team=_no_history_team(1, "Jordan"),
            away_team=_no_history_team(2, "Argentina"),
            home_advantage=0.04,
            external_adjustments={"home_goal_delta": -0.55, "away_goal_delta": 0.55},
        )
        market = self._build_market(home=26.0, draw=12.0, away=1.05)

        result = engine.analyze_fixture(fixture, market)

        # Before fix this was ~18%. After fix it should be much closer to market (~3-7%).
        self.assertLess(result.probabilities.home, 0.10)
        self.assertGreater(result.probabilities.away, 0.80)

    def test_confidence_separates_clear_favorite_from_coin_flip(self) -> None:
        engine = MatchAnalysisEngine()
        # Coin flip (similar implied probabilities)
        coin_flip = engine.analyze_fixture(
            FixtureFeatures(
                fixture_id=201,
                home_team=_no_history_team(3, "A"),
                away_team=_no_history_team(4, "B"),
                home_advantage=0.04,
                external_adjustments={"home_goal_delta": 0.02, "away_goal_delta": -0.02},
            ),
            self._build_market(home=2.85, draw=3.10, away=2.70),
        )
        # Heavy favorite
        clear_favorite = engine.analyze_fixture(
            FixtureFeatures(
                fixture_id=202,
                home_team=_no_history_team(5, "C"),
                away_team=_no_history_team(6, "D"),
                home_advantage=0.04,
                external_adjustments={"home_goal_delta": 0.50, "away_goal_delta": -0.50},
            ),
            self._build_market(home=1.18, draw=7.0, away=14.0),
        )

        cf_confidence = coin_flip.top_selection.confidence_score
        fav_confidence = clear_favorite.top_selection.confidence_score
        self.assertGreater(fav_confidence - cf_confidence, 0.08)


class EdgeCredibilityTest(unittest.TestCase):
    def test_small_edge_with_solid_confidence_is_credible(self) -> None:
        credibility = _edge_credibility(edge_probability=0.06, confidence_score=0.70)
        self.assertGreater(credibility, 0.40)

    def test_huge_edge_is_rejected(self) -> None:
        credibility = _edge_credibility(edge_probability=0.16, confidence_score=0.60)
        # In the 8%-18% decay zone — should be below the recommended threshold.
        self.assertLess(credibility, 0.20)

    def test_edge_above_hard_ceiling_returns_zero(self) -> None:
        credibility = _edge_credibility(edge_probability=EDGE_HARD_CEILING, confidence_score=0.90)
        self.assertEqual(credibility, 0.0)

    def test_recommendation_filters_out_implausible_edges(self) -> None:
        probabilities = OutcomeProbabilities(home=0.30, draw=0.20, away=0.50)
        market = MarketOdds(
            bookmaker="BookA",
            home_odd=20.0,  # implied ~0.05 → edge ~0.25, too good to be true
            draw_odd=4.0,
            away_odd=1.20,
            source_count=1,
        )
        implied = ImpliedProbabilities(home=0.05, draw=0.25, away=0.70, margin=0.04)
        decisions = build_selection_decisions(
            probabilities=probabilities,
            market_odds=market,
            implied_probabilities=implied,
            confidence_score=0.60,
            config=DecisionConfig(),
            consensus_odds=market,
        )
        home_decision = next(d for d in decisions if d.selection_code == "HOME")
        self.assertFalse(home_decision.recommended)


class SingleConvictionPerFixtureTest(unittest.TestCase):
    """Regle 'les MEILLEURS deals' : jamais HOME et AWAY sur le meme match.

    Un double edge H+A signifie un seul desaccord (le modele met moins de
    nul que le marche) — pas deux convictions. Seule la meilleure survit.
    """

    def _dual_edge_setup(self):
        from spe_prediction.domain import ImpliedProbabilities
        # Modele: 52/22/26. Marche: 49/30/21 -> edge positif sur H ET A,
        # entierement finance par le desaccord sur le nul.
        probabilities = OutcomeProbabilities(home=0.52, draw=0.22, away=0.26)
        market = MarketOdds(
            bookmaker="Pinnacle", bookmaker_id=1,
            home_odd=1.96, draw_odd=3.20, away_odd=4.50,
            source_count=15, home_spread=0.04, draw_spread=0.04, away_spread=0.04,
        )
        implied = ImpliedProbabilities(home=0.489, draw=0.298, away=0.213, margin=0.05)
        return probabilities, market, implied

    def test_decision_layer_keeps_only_strongest_side(self) -> None:
        probabilities, market, implied = self._dual_edge_setup()
        decisions = build_selection_decisions(
            probabilities=probabilities,
            market_odds=market,
            implied_probabilities=implied,
            confidence_score=0.70,
            config=DecisionConfig(),
            consensus_odds=market,
        )
        recommended = [d for d in decisions if d.recommended]
        codes = {d.selection_code for d in recommended}
        self.assertFalse({"HOME", "AWAY"} <= codes,
                         f"HOME et AWAY recommandes ensemble: {codes}")
        # L'ecarte porte la trace de la raison.
        discarded = [d for d in decisions if "desaccord sur le nul" in d.rationale]
        self.assertEqual(len(discarded), 1)

    def test_repository_returns_single_selection_per_fixture(self) -> None:
        repo = PostgresPredictionRepository.__new__(PostgresPredictionRepository)
        repo._decision_config = DecisionConfig()
        repo._target_bookmaker = ""
        engine = MatchAnalysisEngine()
        fixture = FixtureFeatures(
            fixture_id=77,
            home_team=TeamStrengthSnapshot(1, "Home", 1700, 0.35, 0.22, played_matches=25, data_quality=0.8),
            away_team=TeamStrengthSnapshot(2, "Away", 1640, 0.25, 0.26, played_matches=25, data_quality=0.8),
        )
        consensus = MarketOdds(
            bookmaker="CONSENSUS", home_odd=2.05, draw_odd=3.35, away_odd=3.90,
            source_count=12, home_spread=0.05, draw_spread=0.05, away_spread=0.05,
        )
        result = engine.analyze_fixture(fixture, consensus)
        offers = tuple(
            MarketOdds(bookmaker=f"B{i}", bookmaker_id=i,
                       home_odd=2.05 + i * 0.03, draw_odd=3.35, away_odd=3.90 + i * 0.05,
                       source_count=1)
            for i in range(6)
        )
        deals = repo._build_deal_candidates(result, offers, consensus)
        selections = {d.selection_code for d in deals}
        self.assertLessEqual(len(selections), 1,
                             f"plusieurs selections sur un meme match: {selections}")
        self.assertLessEqual(len(deals), 2)


class DealDiversificationTest(unittest.TestCase):
    def test_per_selection_cap_avoids_duplicate_stacking(self) -> None:
        repo = PostgresPredictionRepository.__new__(PostgresPredictionRepository)
        repo._decision_config = DecisionConfig()
        engine = MatchAnalysisEngine()

        fixture = FixtureFeatures(
            fixture_id=303,
            home_team=TeamStrengthSnapshot(
                7, "Strong Home", 1700, 0.40, 0.20, recent_form=0.12, played_matches=20, data_quality=0.8
            ),
            away_team=TeamStrengthSnapshot(
                8, "Weak Away", 1550, 0.12, 0.30, recent_form=-0.10, played_matches=20, data_quality=0.8
            ),
        )
        consensus = MarketOdds(
            bookmaker="CONSENSUS",
            home_odd=1.85,
            draw_odd=3.40,
            away_odd=4.50,
            source_count=12,
            home_spread=0.05,
            draw_spread=0.05,
            away_spread=0.06,
        )
        result = engine.analyze_fixture(fixture, consensus)

        # 8 bookmakers all priced very close to consensus on the same selection
        offers = tuple(
            MarketOdds(
                bookmaker=f"Book{i}",
                bookmaker_id=i,
                home_odd=1.85 + (i * 0.01),
                draw_odd=3.40,
                away_odd=4.50,
                source_count=1,
            )
            for i in range(8)
        )

        deals = repo._build_deal_candidates(result, offers, consensus)
        per_selection: dict[str, int] = {}
        for d in deals:
            per_selection[d.selection_code] = per_selection.get(d.selection_code, 0) + 1
        for count in per_selection.values():
            self.assertLessEqual(count, 2)

    def test_freshness_score_reflects_market_spread(self) -> None:
        repo = PostgresPredictionRepository
        tight = MarketOdds(
            bookmaker="C",
            home_odd=2.0,
            draw_odd=3.4,
            away_odd=3.8,
            source_count=10,
            home_spread=0.02,
            draw_spread=0.03,
            away_spread=0.02,
        )
        stale = MarketOdds(
            bookmaker="C",
            home_odd=2.0,
            draw_odd=3.4,
            away_odd=3.8,
            source_count=10,
            home_spread=0.25,
            draw_spread=0.28,
            away_spread=0.27,
        )
        self.assertGreater(repo._fixture_freshness(tight), 0.80)
        self.assertLess(repo._fixture_freshness(stale), 0.20)

    def test_data_quality_blends_team_and_market_coverage(self) -> None:
        repo = PostgresPredictionRepository
        team = TeamStrengthSnapshot(1, "T", 1500, 0.2, 0.2, played_matches=15, data_quality=0.6)
        context = FixtureContext(
            fixture=FixtureFeatures(fixture_id=1, home_team=team, away_team=team),
            market_odds=MarketOdds(
                bookmaker="C", home_odd=2.0, draw_odd=3.4, away_odd=3.8, source_count=8,
            ),
        )
        score = repo._fixture_data_quality(context)
        # 0.55 * 0.6 + 0.45 * 0.8 = 0.69
        self.assertAlmostEqual(score, 0.69, places=2)


if __name__ == "__main__":
    unittest.main()
