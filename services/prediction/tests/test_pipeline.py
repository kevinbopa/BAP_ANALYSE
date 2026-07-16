from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.domain import FixtureFeatures, TeamStrengthSnapshot
from spe_prediction.pipeline import PredictionPipeline
from spe_prediction.repository import FixtureContext, InMemoryPredictionRepository


class PredictionPipelineTest(unittest.TestCase):
    def test_pipeline_persists_all_loaded_fixtures(self) -> None:
        fixture_a = FixtureFeatures(
            fixture_id=11,
            home_team=TeamStrengthSnapshot(1, "Team A", 1700, 0.30, 0.22),
            away_team=TeamStrengthSnapshot(2, "Team B", 1625, 0.12, 0.25),
        )
        fixture_b = FixtureFeatures(
            fixture_id=12,
            home_team=TeamStrengthSnapshot(3, "Team C", 1660, 0.18, 0.18),
            away_team=TeamStrengthSnapshot(4, "Team D", 1675, 0.20, 0.22),
        )
        repository = InMemoryPredictionRepository(
            [FixtureContext(fixture_a), FixtureContext(fixture_b)]
        )

        processed = PredictionPipeline(repository).run()

        self.assertEqual(processed, 2)
        self.assertEqual(len(repository.saved_results), 2)
        self.assertEqual(repository.saved_results[0].fixture_id, 11)
        self.assertEqual(repository.saved_results[1].fixture_id, 12)


if __name__ == "__main__":
    unittest.main()
