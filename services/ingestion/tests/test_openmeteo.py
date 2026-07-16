from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_ingestion.openmeteo import weather_factor


class WeatherFactorTest(unittest.TestCase):
    def test_clement_weather_is_neutral(self) -> None:
        value, note = weather_factor(18.0, 0.0, 10.0)
        self.assertEqual(value, 0.0)
        self.assertEqual(note, "conditions normales")

    def test_heavy_rain_depresses(self) -> None:
        value, note = weather_factor(15.0, 6.0, 10.0)
        self.assertLessEqual(value, -0.45)
        self.assertIn("pluie", note)

    def test_strong_wind_depresses(self) -> None:
        value, note = weather_factor(15.0, 0.0, 55.0)
        self.assertLessEqual(value, -0.30)
        self.assertIn("vent", note)

    def test_extreme_heat_and_cold(self) -> None:
        hot, note_hot = weather_factor(38.0, 0.0, 5.0)
        cold, note_cold = weather_factor(-10.0, 0.0, 5.0)
        self.assertLess(hot, 0.0)
        self.assertLess(cold, 0.0)
        self.assertIn("chaleur", note_hot)
        self.assertIn("froid", note_cold)

    def test_combined_storm_is_capped_at_minus_one(self) -> None:
        value, _note = weather_factor(-15.0, 10.0, 70.0)
        self.assertGreaterEqual(value, -1.0)

    def test_missing_data_is_neutral(self) -> None:
        value, _ = weather_factor(None, None, None)
        self.assertEqual(value, 0.0)


if __name__ == "__main__":
    unittest.main()
