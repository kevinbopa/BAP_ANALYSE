from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_ingestion.thesportsdb_ingestor import (
    _build_timestamp,
    _is_target_event,
    _is_target_league,
    _normalize_url,
    _season_start_year,
    _to_bool,
    _to_int,
    _winner_code,
)


class TheSportsDBIngestorHelpersTest(unittest.TestCase):
    def test_int_and_bool_helpers(self) -> None:
        self.assertEqual(_to_int("12"), 12)
        self.assertIsNone(_to_int(""))
        self.assertTrue(_to_bool("1"))
        self.assertFalse(_to_bool("0"))

    def test_normalize_url(self) -> None:
        self.assertEqual(_normalize_url("example.com"), "https://example.com")
        self.assertEqual(_normalize_url("https://example.com"), "https://example.com")
        self.assertIsNone(_normalize_url(""))

    def test_build_timestamp_and_winner(self) -> None:
        timestamp = _build_timestamp("2026-06-27", "18:30:00")
        self.assertIsNotNone(timestamp)
        self.assertEqual(_winner_code("2", "1"), "HOME")
        self.assertEqual(_winner_code("1", "1"), "DRAW")
        self.assertEqual(_winner_code("0", "3"), "AWAY")

    def test_season_start_year_and_target_leagues(self) -> None:
        self.assertEqual(_season_start_year("2010-2011"), 2010)
        self.assertEqual(_season_start_year("2024"), 2024)
        self.assertIsNone(_season_start_year(""))

        world_cup = {
            "strSport": "Soccer",
            "strLeague": "FIFA World Cup",
            "strLeagueAlternate": "",
            "strCountry": "World",
            "intFormedYear": "1930",
        }
        international_friendly = {
            "strSport": "Soccer",
            "strLeague": "International Friendly Games",
            "strLeagueAlternate": "",
            "strCountry": "World",
            "intFormedYear": "1990",
        }
        domestic_other = {
            "strSport": "Soccer",
            "strLeague": "Random Local League",
            "strLeagueAlternate": "",
            "strCountry": "England",
            "intFormedYear": "1980",
        }

        self.assertTrue(_is_target_league(world_cup))
        self.assertTrue(_is_target_league(international_friendly))
        self.assertFalse(_is_target_league(domestic_other))

    def test_excluded_variants_rejected_even_with_matching_keywords(self) -> None:
        # Une cle premium expose la liste complete des ligues : les variantes
        # feminines / beach / futsal / esport contiennent aussi "world cup"
        # et doivent etre rejetees (scope V1 = football masculin senior).
        variants = [
            "Womens World Cup Qualifying CONCACAF",
            "FIFA Womens World Cup",
            "Beach Soccer World Cup",
            "Futsal World Cup",
            "FIFAe World Cup",
        ]
        for name in variants:
            league = {
                "strSport": "Soccer",
                "strLeague": name,
                "strLeagueAlternate": "",
                "strCountry": "World",
                "intFormedYear": "2000",
            }
            self.assertFalse(_is_target_league(league), f"should reject: {name}")

        # Le pendant masculin senior reste accepte.
        mens_wcq = {
            "strSport": "Soccer",
            "strLeague": "World Cup Qualifying CONCACAF",
            "strLeagueAlternate": "",
            "strCountry": "World",
            "intFormedYear": "2000",
        }
        self.assertTrue(_is_target_league(mens_wcq))

    def test_premium_slim_league_payload_matches_on_keywords_alone(self) -> None:
        # La liste premium all_leagues.php n'inclut ni strCountry ni
        # intFormedYear — les mots-cles doivent suffire.
        slim_friendlies = {
            "strSport": "Soccer",
            "strLeague": "International Friendlies",
            "strLeagueAlternate": "",
        }
        slim_wcq = {
            "strSport": "Soccer",
            "strLeague": "World Cup Qualifying UEFA",
            "strLeagueAlternate": "",
        }
        slim_club_comp = {
            "strSport": "Soccer",
            "strLeague": "AFC Challenge League",
            "strLeagueAlternate": "",
        }
        self.assertTrue(_is_target_league(slim_friendlies))
        self.assertTrue(_is_target_league(slim_wcq))
        self.assertFalse(_is_target_league(slim_club_comp))

    def test_event_level_filter_rejects_hidden_womens_and_corrupt_rows(self) -> None:
        # Ligues mixtes : des matchs feminins se cachent dans International
        # Friendlies (masculin). Vu en production: "England Women vs England Women".
        womens_event = {
            "strEvent": "England Women vs England Women",
            "strHomeTeam": "England Women",
            "strAwayTeam": "England Women",
        }
        mens_event = {
            "strEvent": "England vs Brazil",
            "strHomeTeam": "England",
            "strAwayTeam": "Brazil",
        }
        youth_event = {
            "strEvent": "France U-16 vs Spain U-16",
            "strHomeTeam": "France U-16",
            "strAwayTeam": "Spain U-16",
        }
        self.assertFalse(_is_target_event(womens_event))
        self.assertTrue(_is_target_event(mens_event))
        self.assertFalse(_is_target_event(youth_event))


if __name__ == "__main__":
    unittest.main()
