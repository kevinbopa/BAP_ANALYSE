"""Tests for the target-bookmaker deal surfacing (bet365 by default).

Business rule: the model's consensus is computed from ALL bookmakers, but the
deals shown to the user come from the bookmaker they actually bet on. When the
target book does not price a fixture, we fall back to every book rather than
showing nothing.
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
from spe_prediction.repository import PostgresPredictionRepository


def _team(team_id: int, name: str) -> TeamStrengthSnapshot:
    return TeamStrengthSnapshot(
        team_id=team_id,
        team_name=name,
        elo_rating=1700.0 if team_id % 2 else 1560.0,
        attack_rating=0.40 if team_id % 2 else 0.15,
        defense_rating=0.22 if team_id % 2 else 0.30,
        recent_form=0.15 if team_id % 2 else -0.05,
        played_matches=20,
        data_quality=0.8,
    )


def _repo(target: str) -> PostgresPredictionRepository:
    repo = PostgresPredictionRepository.__new__(PostgresPredictionRepository)
    repo._decision_config = DecisionConfig()
    repo._target_bookmaker = target
    return repo


def _analyze():
    engine = MatchAnalysisEngine()
    fixture = FixtureFeatures(
        fixture_id=7,
        home_team=_team(1, "Strong Home"),
        away_team=_team(2, "Weak Away"),
    )
    consensus = MarketOdds(
        bookmaker="CONSENSUS",
        home_odd=1.90,
        draw_odd=3.40,
        away_odd=4.40,
        source_count=10,
        home_spread=0.05,
        draw_spread=0.05,
        away_spread=0.06,
    )
    return engine.analyze_fixture(fixture, consensus), consensus


def _offers(include_bet365: bool) -> tuple[MarketOdds, ...]:
    books = [
        ("Pinnacle", 1),
        ("Betsson", 2),
        ("Unibet (FR)", 3),
        ("Matchbook", 4),
    ]
    if include_bet365:
        books.append(("Bet365", 5))
    return tuple(
        MarketOdds(
            bookmaker=name,
            bookmaker_id=bid,
            home_odd=1.90 + bid * 0.02,
            draw_odd=3.40,
            away_odd=4.40,
            source_count=1,
        )
        for name, bid in books
    )


class MatchesTargetBookmakerTest(unittest.TestCase):
    def test_case_and_spacing_insensitive(self) -> None:
        m = PostgresPredictionRepository._matches_target_bookmaker
        self.assertTrue(m("Bet365", "bet365"))
        self.assertTrue(m("BET 365", "bet365"))
        self.assertTrue(m("bet-365", "bet365"))
        self.assertFalse(m("Betsson", "bet365"))
        self.assertFalse(m(None, "bet365"))
        self.assertFalse(m("Bet365", ""))


class TargetBookmakerDealsTest(unittest.TestCase):
    def test_deals_only_from_target_when_it_prices_the_fixture(self) -> None:
        result, consensus = _analyze()
        repo = _repo("bet365")
        deals = repo._build_deal_candidates(result, _offers(include_bet365=True), consensus)
        self.assertTrue(deals, "expected at least one deal in this setup")
        for deal in deals:
            self.assertIn("bet365", (deal.bookmaker or "").lower().replace(" ", ""))

    def test_fallback_to_all_books_when_target_missing(self) -> None:
        result, consensus = _analyze()
        repo = _repo("bet365")
        deals = repo._build_deal_candidates(result, _offers(include_bet365=False), consensus)
        self.assertTrue(deals, "fallback must still surface deals from other books")
        bookmakers = {(deal.bookmaker or "").lower() for deal in deals}
        self.assertNotIn("bet365", bookmakers)

    def test_empty_target_keeps_all_books(self) -> None:
        result, consensus = _analyze()
        repo = _repo("")
        deals = repo._build_deal_candidates(result, _offers(include_bet365=True), consensus)
        self.assertTrue(deals)


if __name__ == "__main__":
    unittest.main()
