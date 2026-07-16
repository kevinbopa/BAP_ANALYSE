"""Tests for the exponential-decay temporal weighting and the
hierarchical competition multipliers."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.repository import PostgresPredictionRepository


REFERENCE_NOW = datetime(2026, 6, 27, tzinfo=timezone.utc)


def _row(days_ago: int, league: str, gf: int, ga: int, points: float) -> tuple:
    kickoff = REFERENCE_NOW - timedelta(days=days_ago)
    return (kickoff, league, gf, ga, points)


class TemporalDecayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = PostgresPredictionRepository.__new__(PostgresPredictionRepository)
        self.repo._RECENCY_NOW_OVERRIDE = REFERENCE_NOW

    def test_recency_weight_halves_after_one_year(self) -> None:
        fresh = self.repo._recency_weight(REFERENCE_NOW, REFERENCE_NOW)
        one_year = self.repo._recency_weight(REFERENCE_NOW - timedelta(days=365), REFERENCE_NOW)
        two_years = self.repo._recency_weight(REFERENCE_NOW - timedelta(days=730), REFERENCE_NOW)
        self.assertAlmostEqual(fresh, 1.0, places=6)
        self.assertAlmostEqual(one_year, 0.5, places=3)
        self.assertAlmostEqual(two_years, 0.25, places=3)

    def test_recent_wins_dominate_over_distant_losses(self) -> None:
        rows = [
            _row(15, "FIFA World Cup Qualification", 3, 0, 1.0),
            _row(45, "UEFA Nations League", 2, 1, 1.0),
            _row(90, "International Friendly Games", 1, 1, 0.5),
            _row(900, "FIFA World Cup Qualification", 0, 4, 0.0),
            _row(1500, "International Friendly Games", 0, 3, 0.0),
        ]
        metrics = self.repo._compute_layered_team_metrics(rows)
        self.assertGreater(metrics["recent_form"], 0.30)
        self.assertGreater(metrics["result_points"], 0.65)

    def test_data_quality_grows_with_recent_volume(self) -> None:
        sparse = [_row(20, "International Friendly Games", 1, 1, 0.5)]
        rich = [_row(10 + i * 30, "FIFA World Cup Qualification", 1, 1, 0.5) for i in range(14)]
        sparse_quality = self.repo._compute_layered_team_metrics(sparse)["data_quality"]
        rich_quality = self.repo._compute_layered_team_metrics(rich)["data_quality"]
        self.assertGreater(rich_quality, sparse_quality)
        self.assertGreater(rich_quality, 0.6)

    def test_old_matches_alone_yield_low_data_quality(self) -> None:
        only_old = [_row(1500 + i * 30, "FIFA World Cup Qualification", 1, 1, 0.5) for i in range(8)]
        metrics = self.repo._compute_layered_team_metrics(only_old)
        # 4+ years old → weight per match ~ 0.06, total ~ 0.5 → data_quality ~ 0.05
        self.assertLess(metrics["data_quality"], 0.20)


class CompetitionHierarchyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = PostgresPredictionRepository.__new__(PostgresPredictionRepository)

    def test_senior_world_cup_outranks_qualifier(self) -> None:
        self.assertGreater(
            self.repo._competition_weight("FIFA World Cup"),
            self.repo._competition_weight("FIFA World Cup Qualification"),
        )

    def test_continental_seniors_outrank_friendlies(self) -> None:
        for comp in ("UEFA European Championship", "Copa America", "Africa Cup of Nations", "AFC Asian Cup"):
            self.assertGreater(
                self.repo._competition_weight(comp),
                self.repo._competition_weight("International Friendly Games"),
            )

    def test_youth_competitions_are_downweighted(self) -> None:
        self.assertLess(self.repo._competition_weight("FIFA U-20 World Cup"), 0.80)
        self.assertLess(self.repo._competition_weight("UEFA U21 Championship"), 0.80)

    def test_nations_league_above_baseline_below_majors(self) -> None:
        baseline = self.repo._competition_weight("English Premier League")
        nations = self.repo._competition_weight("UEFA Nations League")
        major = self.repo._competition_weight("FIFA World Cup")
        self.assertGreater(nations, baseline)
        self.assertLess(nations, major)


if __name__ == "__main__":
    unittest.main()
