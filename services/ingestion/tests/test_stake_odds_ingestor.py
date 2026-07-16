from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_ingestion.odds_matching import compare_team_names
from spe_ingestion.stake_odds_ingestor import extract_1x2_odds


class StakeOddsIngestorHelpersTest(unittest.TestCase):
    def test_compare_team_names_handles_common_aliases(self) -> None:
        self.assertGreater(compare_team_names("Inter Milan", "Inter"), 0)
        self.assertGreater(compare_team_names("Borussia Mönchengladbach", "Monchengladbach"), 0)
        self.assertGreater(compare_team_names("Atlético Madrid", "Atletico Madrid"), 0)

    def test_extract_1x2_odds_from_threeway_market(self) -> None:
        payload = {
            "fixture": {
                "updatedAt": 1782361677399,
            },
            "groups": [
                {
                    "name": "threeway",
                    "markets": [
                        [
                            {
                                "name": "Match Winner",
                                "updatedAt": 1782503465210,
                                "outcomes": [
                                    {"name": "Frosinone", "odds": 4.4},
                                    {"name": "Draw", "odds": 3.7},
                                    {"name": "Juventus", "odds": 1.78},
                                ],
                            }
                        ]
                    ],
                }
            ],
        }

        extracted = extract_1x2_odds(
            payload=payload,
            home_team_name="Frosinone",
            away_team_name="Juventus",
        )

        self.assertIsNotNone(extracted)
        assert extracted is not None
        self.assertEqual(extracted.home_odd, 4.4)
        self.assertEqual(extracted.draw_odd, 3.7)
        self.assertEqual(extracted.away_odd, 1.78)
        self.assertEqual(extracted.group_name, "threeway")
        self.assertEqual(extracted.market_name, "Match Winner")
        self.assertEqual(
            extracted.captured_at,
            datetime.fromtimestamp(1782503465210 / 1000.0, tz=UTC),
        )

    def test_extract_1x2_odds_returns_none_when_market_is_missing(self) -> None:
        payload = {
            "fixture": {
                "updatedAt": 1782361677399,
            },
            "groups": [
                {
                    "name": "goals",
                    "markets": [
                        [
                            {
                                "name": "Juventus Total",
                                "updatedAt": 1782503465210,
                                "outcomes": [
                                    {"name": "Over 1.5", "odds": 1.64},
                                    {"name": "Under 1.5", "odds": 2.23},
                                ],
                            }
                        ]
                    ],
                }
            ],
        }

        extracted = extract_1x2_odds(
            payload=payload,
            home_team_name="Frosinone",
            away_team_name="Juventus",
        )
        self.assertIsNone(extracted)


if __name__ == "__main__":
    unittest.main()
