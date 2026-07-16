from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_ingestion.thesportsdb_player_ingestor import (
    _position_code,
    _timeline_code,
    _to_bool_yesno,
    _to_date,
    _to_float,
    _to_int,
    extract_event_context,
)


class ExtractEventContextTest(unittest.TestCase):
    def test_rich_event_extracts_all_context(self) -> None:
        # Fiche reelle de la finale WC 2022 (champs pertinents)
        event = {
            "intSpectators": "88966",
            "strWeather": "Overcast",
            "strCity": "Lusail",
            "strCountry": "Qatar",
            "strVenue": "Lusail Stadium",
        }
        context = extract_event_context(event)
        self.assertEqual(context["attendance"], 88966)
        self.assertEqual(context["weather_text"], "Overcast")
        self.assertEqual(context["venue_city"], "Lusail")
        self.assertEqual(context["venue_country"], "Qatar")

    def test_sparse_event_returns_only_filled_fields(self) -> None:
        # Vieux match mineur : fiche quasi vide -> on n'ecrase rien avec du vide.
        event = {"intSpectators": None, "strWeather": "", "strCity": None}
        self.assertEqual(extract_event_context(event), {})

    def test_zero_attendance_is_ignored(self) -> None:
        self.assertEqual(extract_event_context({"intSpectators": "0"}), {})

    def test_extract_event_venue(self) -> None:
        from spe_ingestion.thesportsdb_player_ingestor import extract_event_venue
        venue = extract_event_venue({"idVenue": "28836", "strVenue": "Lusail Stadium"})
        self.assertEqual(venue["thesportsdb_venue_id"], 28836)
        self.assertEqual(venue["venue_name"], "Lusail Stadium")
        # Fiche sans stade -> rien (on ne cree pas de stade vide)
        self.assertEqual(extract_event_venue({"idVenue": "0", "strVenue": ""}), {})


class TimelineCodeTest(unittest.TestCase):
    def test_goal_variants(self) -> None:
        self.assertEqual(_timeline_code("Goal"), "GOAL")
        self.assertEqual(_timeline_code("goal"), "GOAL")
        self.assertEqual(_timeline_code("Own Goal"), "GOAL")

    def test_cards_subs_var(self) -> None:
        self.assertEqual(_timeline_code("Card"), "CARD")
        self.assertEqual(_timeline_code("Yellow Card"), "CARD")
        self.assertEqual(_timeline_code("subst"), "SUB")
        self.assertEqual(_timeline_code("Substitution"), "SUB")
        self.assertEqual(_timeline_code("VAR"), "VAR")

    def test_unknown_is_other(self) -> None:
        self.assertEqual(_timeline_code("Trophy Lift"), "OTHER")
        self.assertEqual(_timeline_code(None), "OTHER")


class ParsingHelpersTest(unittest.TestCase):
    def test_yes_no(self) -> None:
        self.assertTrue(_to_bool_yesno("Yes"))
        self.assertTrue(_to_bool_yesno("yes"))
        self.assertFalse(_to_bool_yesno("No"))
        self.assertFalse(_to_bool_yesno(None))

    def test_position_mapping(self) -> None:
        self.assertEqual(_position_code("Goalkeeper"), "G")
        self.assertEqual(_position_code("Midfielder"), "M")
        self.assertEqual(_position_code("Forward"), "F")
        self.assertIsNone(_position_code(""))

    def test_numeric_tolerance(self) -> None:
        self.assertEqual(_to_int("7"), 7)
        self.assertIsNone(_to_int("N/A"))
        self.assertEqual(_to_float("64%"), 64.0)
        self.assertIsNone(_to_float(""))

    def test_date_tolerance(self) -> None:
        self.assertIsNotNone(_to_date("1998-12-20"))
        self.assertIsNone(_to_date("unknown"))
        self.assertIsNone(_to_date(None))


if __name__ == "__main__":
    unittest.main()
