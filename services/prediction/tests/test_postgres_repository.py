from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.domain import OutcomeProbabilities
from spe_prediction.examples import build_example_result
from spe_prediction.serialization import prediction_record


class RepositorySupportTest(unittest.TestCase):
    def test_prediction_record_uses_expected_model_identity(self) -> None:
        result = build_example_result()
        record = prediction_record(result, model_run_id="test-run")

        self.assertEqual(record["model_name"], "poisson_market_hybrid_v1")
        self.assertEqual(record["model_version"], "1.2.0")
        self.assertEqual(record["model_run_id"], "test-run")

    def test_probabilities_dict_shape(self) -> None:
        probs = OutcomeProbabilities(home=0.4, draw=0.3, away=0.3)
        self.assertEqual(probs.as_dict()["HOME"], 0.4)
        self.assertEqual(probs.as_dict()["DRAW"], 0.3)
        self.assertEqual(probs.as_dict()["AWAY"], 0.3)


if __name__ == "__main__":
    unittest.main()
