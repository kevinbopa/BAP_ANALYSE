from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_prediction.examples import build_example_exact_score_result


def main() -> int:
    result = build_example_exact_score_result()
    payload = {
        "fixture_id": result.fixture_id,
        "expected_home_goals": result.match_analysis.expected_home_goals,
        "expected_away_goals": result.match_analysis.expected_away_goals,
        "top_scores": [
            {
                "score": row.score_label,
                "probability": round(row.probability, 6),
                "fair_odd": round(row.fair_odd, 3),
                "outcome_code": row.outcome_code,
            }
            for row in result.top_scores
        ],
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
