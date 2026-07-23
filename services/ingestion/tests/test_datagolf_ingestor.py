from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from services.ingestion.run_ingest_datagolf import _resolve_player


class DataGolfIngestorHelpersTest(unittest.TestCase):
    def test_resolve_player_only_accepts_players_from_field_map(self) -> None:
        field_map = {123: 77}

        self.assertEqual(_resolve_player(None, field_map, 123, "Player In"), 77)
        self.assertIsNone(_resolve_player(None, field_map, 999, "Player Out"))
        self.assertIsNone(_resolve_player(None, field_map, None, "Missing"))


if __name__ == "__main__":
    unittest.main()
