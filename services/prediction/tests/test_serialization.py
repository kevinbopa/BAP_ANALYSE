from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.examples import build_example_exact_score_result, build_example_result
from spe_prediction.serialization import prediction_record, ranking_record, value_bet_record


class SerializationTest(unittest.TestCase):
    def test_prediction_record_contains_expected_keys(self) -> None:
        result = build_example_result()
        record = prediction_record(result)

        self.assertEqual(record["fixture_id"], result.fixture_id)
        self.assertIn("home_win_probability", record)
        self.assertIn("feature_snapshot_json", record)

    def test_prediction_record_can_embed_exact_scores(self) -> None:
        exact = build_example_exact_score_result()
        record = prediction_record(exact.match_analysis, exact_score_analysis=exact)
        self.assertIn("feature_snapshot_json", record)
        self.assertIn("exact_scores", record["feature_snapshot_json"])

    def test_value_bet_and_ranking_records_can_be_built(self) -> None:
        result = build_example_result()
        selection = result.top_selection

        value_bet = value_bet_record(result, selection, prediction_id=99, bookmaker_id=7)
        ranking = ranking_record(
            result,
            selection,
            prediction_id=99,
            rank_position=1,
            analysis_scope_id="00000000-0000-0000-0000-000000000001",
            value_bet_id=55,
        )

        self.assertEqual(value_bet["prediction_id"], 99)
        self.assertEqual(value_bet["bookmaker_id"], 7)
        self.assertEqual(ranking["rank_position"], 1)
        self.assertEqual(ranking["value_bet_id"], 55)


if __name__ == "__main__":
    unittest.main()
