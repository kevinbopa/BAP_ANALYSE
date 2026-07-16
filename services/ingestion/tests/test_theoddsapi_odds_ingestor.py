from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_ingestion.odds_matching import compare_team_names
from spe_ingestion.theoddsapi_odds_ingestor import (
    extract_1x2_rows,
    extract_btts_rows,
    extract_totals_rows,
)


class TheOddsApiOddsIngestorHelpersTest(unittest.TestCase):
    def test_compare_team_names_uses_shared_alias_logic(self) -> None:
        self.assertGreater(compare_team_names("Inter Milan", "Inter"), 0)
        self.assertGreater(compare_team_names("Atlético Madrid", "Atletico Madrid"), 0)

    def test_extract_1x2_rows_reads_h2h_market(self) -> None:
        event = {
            "id": "evt_123",
            "home_team": "Juventus",
            "away_team": "Inter Milan",
            "bookmakers": [
                {
                    "key": "bet365",
                    "title": "Bet365",
                    "last_update": "2026-06-27T16:40:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Juventus", "price": 2.4},
                                {"name": "Draw", "price": 3.2},
                                {"name": "Inter", "price": 2.9},
                            ],
                        }
                    ],
                }
            ],
        }

        rows = extract_1x2_rows(
            event=event,
            home_team_name="Juventus",
            away_team_name="Inter Milan",
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.bookmaker_code, "bet365")
        self.assertEqual(row.bookmaker_name, "Bet365")
        self.assertEqual(row.home_odd, 2.4)
        self.assertEqual(row.draw_odd, 3.2)
        self.assertEqual(row.away_odd, 2.9)
        self.assertEqual(row.source_reference, "evt_123|bet365|h2h")
        self.assertEqual(
            row.captured_at,
            datetime.fromisoformat("2026-06-27T16:40:00+00:00").astimezone(UTC),
        )

    def test_extract_1x2_rows_skips_two_way_market(self) -> None:
        event = {
            "id": "evt_456",
            "home_team": "Barcelona",
            "away_team": "Real Madrid",
            "bookmakers": [
                {
                    "key": "betfair",
                    "title": "Betfair",
                    "last_update": "2026-06-27T16:45:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Barcelona", "price": 1.9},
                                {"name": "Real Madrid", "price": 1.95},
                            ],
                        }
                    ],
                }
            ],
        }

        rows = extract_1x2_rows(
            event=event,
            home_team_name="Barcelona",
            away_team_name="Real Madrid",
        )

        self.assertEqual(rows, [])

    def test_extract_1x2_rows_filters_non_target_bookmakers(self) -> None:
        event = {
            "id": "evt_789",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "bookmakers": [
                {
                    "key": "bet365",
                    "title": "Bet365",
                    "last_update": "2026-06-27T16:40:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 2.1},
                                {"name": "Draw", "price": 3.5},
                                {"name": "Chelsea", "price": 3.3},
                            ],
                        }
                    ],
                },
                {
                    "key": "betfair",
                    "title": "Betfair",
                    "last_update": "2026-06-27T16:41:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 2.2},
                                {"name": "Draw", "price": 3.4},
                                {"name": "Chelsea", "price": 3.2},
                            ],
                        }
                    ],
                },
            ],
        }

        rows = extract_1x2_rows(
            event=event,
            home_team_name="Arsenal",
            away_team_name="Chelsea",
            allowed_bookmakers={"bet365"},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].bookmaker_code, "bet365")


class DerivedMarketsExtractionTest(unittest.TestCase):
    def _event(self) -> dict:
        return {
            "id": "evt_totals",
            "home_team": "Juventus",
            "away_team": "Inter Milan",
            "bookmakers": [
                {
                    "key": "pinnacle",
                    "title": "Pinnacle",
                    "last_update": "2026-06-27T16:40:00Z",
                    "markets": [
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "price": 1.87, "point": 2.5},
                                {"name": "Under", "price": 1.95, "point": 2.5},
                                {"name": "Over", "price": 1.30, "point": 1.5},
                                {"name": "Under", "price": 3.40, "point": 1.5},
                            ],
                        },
                        {
                            "key": "btts",
                            "outcomes": [
                                {"name": "Yes", "price": 1.72},
                                {"name": "No", "price": 2.10},
                            ],
                        },
                    ],
                }
            ],
        }

    def test_extract_totals_groups_by_line(self) -> None:
        rows = extract_totals_rows(event=self._event())
        self.assertEqual(len(rows), 2)
        line_25 = next(r for r in rows if r.total_line == 2.5)
        self.assertEqual(line_25.over_odd, 1.87)
        self.assertEqual(line_25.under_odd, 1.95)
        self.assertEqual(line_25.source_reference, "evt_totals|pinnacle|totals|2.5")
        line_15 = next(r for r in rows if r.total_line == 1.5)
        self.assertEqual(line_15.over_odd, 1.30)

    def test_extract_totals_skips_incomplete_line(self) -> None:
        event = self._event()
        # Retire le Under 2.5 : la ligne 2.5 devient orpheline -> ignoree.
        outcomes = event["bookmakers"][0]["markets"][0]["outcomes"]
        event["bookmakers"][0]["markets"][0]["outcomes"] = [
            o for o in outcomes if not (o["name"] == "Under" and o["point"] == 2.5)
        ]
        rows = extract_totals_rows(event=event)
        self.assertEqual([r.total_line for r in rows], [1.5])

    def test_extract_btts_reads_yes_no(self) -> None:
        rows = extract_btts_rows(event=self._event())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].yes_odd, 1.72)
        self.assertEqual(rows[0].no_odd, 2.10)
        self.assertEqual(rows[0].source_reference, "evt_totals|pinnacle|btts")

    def test_bookmaker_filter_applies(self) -> None:
        self.assertEqual(
            extract_totals_rows(event=self._event(), allowed_bookmakers={"bet365"}), []
        )
        self.assertEqual(
            extract_btts_rows(event=self._event(), allowed_bookmakers={"bet365"}), []
        )

    def test_event_without_derived_markets(self) -> None:
        event = {"id": "evt_h2h_only", "bookmakers": [
            {"key": "pinnacle", "title": "Pinnacle", "markets": [{"key": "h2h", "outcomes": []}]}
        ]}
        self.assertEqual(extract_totals_rows(event=event), [])
        self.assertEqual(extract_btts_rows(event=event), [])


if __name__ == "__main__":
    unittest.main()
