from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.repository import PostgresPredictionRepository


class RepositoryLayeringTest(unittest.TestCase):
    def test_three_layer_weighting_favors_recent_matches(self) -> None:
        repository = PostgresPredictionRepository.__new__(PostgresPredictionRepository)
        rows = [
            (datetime(2026, 6, 20, tzinfo=timezone.utc), "FIFA World Cup", 3, 0, 1.0),
            (datetime(2026, 6, 15, tzinfo=timezone.utc), "FIFA World Cup", 2, 0, 1.0),
            (datetime(2026, 6, 10, tzinfo=timezone.utc), "International Friendly Games", 1, 1, 0.5),
            (datetime(2026, 6, 5, tzinfo=timezone.utc), "UEFA Nations League", 2, 1, 1.0),
            (datetime(2026, 6, 1, tzinfo=timezone.utc), "UEFA Nations League", 1, 0, 1.0),
            (datetime(2025, 10, 1, tzinfo=timezone.utc), "UEFA Nations League", 0, 2, 0.0),
            (datetime(2025, 9, 1, tzinfo=timezone.utc), "International Friendly Games", 0, 1, 0.0),
            (datetime(2025, 8, 1, tzinfo=timezone.utc), "International Friendly Games", 1, 2, 0.0),
            (datetime(2025, 7, 1, tzinfo=timezone.utc), "FIFA World Cup Qualification", 1, 1, 0.5),
            (datetime(2024, 7, 1, tzinfo=timezone.utc), "FIFA World Cup Qualification", 0, 0, 0.5),
        ]

        metrics = repository._compute_layered_team_metrics(rows)

        self.assertGreater(metrics["goals_for"], 1.0)
        self.assertLess(metrics["goals_against"], 1.2)
        self.assertGreater(metrics["recent_form"], 0.2)
        self.assertGreater(metrics["result_points"], 0.55)
        self.assertGreater(metrics["data_quality"], 0.5)

    def test_competition_weight_penalizes_friendlies_vs_major_tournaments(self) -> None:
        repository = PostgresPredictionRepository.__new__(PostgresPredictionRepository)
        self.assertLess(repository._competition_weight("International Friendly Games"), 1.0)
        self.assertGreater(repository._competition_weight("FIFA World Cup"), 1.0)
        self.assertEqual(repository._competition_weight("English Premier League"), 1.0)


if __name__ == "__main__":
    unittest.main()
