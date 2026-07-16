"""Regression test reproducing the production bug where every top deal came from
the same two matches (Jordan-Argentina HOME, Argentina-CapeVerde AWAY).

The bug had two compounding causes:
  1. Engine kept ~57% weight on Poisson/Elo when team history was empty,
     producing 18.7% HOME for Jordan vs Argentina (market: 3.8%).
  2. Decision layer rewarded huge edges instead of treating them as model errors.

This test simulates the dashboard's deal-ranking step on the exact World Cup
fixtures from the bug screenshot and asserts that no single fixture monopolises
the ranking anymore.
"""
from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.domain import (
    DecisionConfig,
    FixtureFeatures,
    MarketOdds,
    TeamStrengthSnapshot,
)
from spe_prediction.engine import MatchAnalysisEngine
from spe_prediction.repository import FixtureContext, PostgresPredictionRepository


def _empty_history_team(team_id: int, name: str) -> TeamStrengthSnapshot:
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


def _book_offers(home: float, draw: float, away: float, count: int = 8) -> tuple[MarketOdds, ...]:
    """Mimic 8 bookmakers each priced around the same consensus."""
    return tuple(
        MarketOdds(
            bookmaker=f"Book{i}",
            bookmaker_id=i,
            home_odd=home + i * 0.05,
            draw_odd=draw + i * 0.05,
            away_odd=away - i * 0.005,
            source_count=1,
        )
        for i in range(count)
    )


def _consensus(home: float, draw: float, away: float, count: int) -> MarketOdds:
    return MarketOdds(
        bookmaker="CONSENSUS",
        home_odd=home,
        draw_odd=draw,
        away_odd=away,
        source_count=count,
        home_spread=0.05,
        draw_spread=0.04,
        away_spread=0.03,
    )


class ArgentinaStackingRegressionTest(unittest.TestCase):
    def test_no_single_fixture_dominates_top_deals(self) -> None:
        engine = MatchAnalysisEngine()
        repo = PostgresPredictionRepository.__new__(PostgresPredictionRepository)
        repo._decision_config = DecisionConfig()

        # World Cup matches from the production screenshot, all with empty team history.
        fixtures = [
            # (fixture_id, home_name, away_name, home_odd, draw_odd, away_odd, home_delta, away_delta)
            (101, "Jordan", "Argentina", 26.00, 12.0, 1.05, -0.55, 0.55),
            (102, "Argentina", "Cape Verde", 1.18, 7.0, 14.0, 0.50, -0.50),
            (103, "Brazil", "Japan", 1.50, 4.5, 6.0, 0.35, -0.35),
            (104, "Germany", "Paraguay", 1.55, 4.2, 5.8, 0.30, -0.30),
            (105, "France", "Sweden", 1.48, 4.6, 6.2, 0.32, -0.32),
            (106, "USA", "Bosnia & Herzegovina", 1.52, 4.4, 5.9, 0.30, -0.30),
            (107, "Croatia", "Ghana", 1.85, 3.4, 4.2, 0.18, -0.18),
        ]

        all_deals: list[tuple[int, str, str, float]] = []
        for fixture_id, home_name, away_name, home, draw, away, hd, ad in fixtures:
            fixture = FixtureFeatures(
                fixture_id=fixture_id,
                home_team=_empty_history_team(fixture_id * 10, home_name),
                away_team=_empty_history_team(fixture_id * 10 + 1, away_name),
                home_advantage=0.04,
                external_adjustments={"home_goal_delta": hd, "away_goal_delta": ad},
            )
            consensus = _consensus(home, draw, away, count=8)
            result = engine.analyze_fixture(fixture, consensus)
            offers = _book_offers(home, draw, away, count=8)
            deals = repo._build_deal_candidates(result, offers, consensus)
            for deal in deals:
                all_deals.append((fixture_id, home_name, deal.selection_code, deal.ranking_score))

        # Sort by ranking score and inspect top 12 globally
        all_deals.sort(key=lambda x: x[3], reverse=True)
        top_12 = all_deals[:12]

        # No single fixture may take more than 4 of the top 12 deal slots.
        per_fixture = {}
        for fixture_id, *_rest in top_12:
            per_fixture[fixture_id] = per_fixture.get(fixture_id, 0) + 1
        for count in per_fixture.values():
            self.assertLessEqual(
                count, 4,
                f"A single fixture dominates the ranking: {per_fixture}",
            )

        # The Jordan upset must NOT appear in the top deals: edge above hard ceiling.
        jordan_deals = [d for d in all_deals if d[1] == "Jordan" and d[2] == "HOME"]
        self.assertEqual(len(jordan_deals), 0)

    def test_jordan_home_probability_collapses_to_market_level(self) -> None:
        engine = MatchAnalysisEngine()
        fixture = FixtureFeatures(
            fixture_id=999,
            home_team=_empty_history_team(1, "Jordan"),
            away_team=_empty_history_team(2, "Argentina"),
            home_advantage=0.04,
            external_adjustments={"home_goal_delta": -0.55, "away_goal_delta": 0.55},
        )
        consensus = _consensus(26.0, 12.0, 1.05, count=12)
        result = engine.analyze_fixture(fixture, consensus)

        # Bug screenshot showed 18.7%. After fix it must be < 10%.
        self.assertLess(result.probabilities.home, 0.10)
        # And the model should not flag Jordan-HOME as recommended.
        jordan_home = next(s for s in result.selections if s.selection_code == "HOME")
        self.assertFalse(jordan_home.recommended)


if __name__ == "__main__":
    unittest.main()
