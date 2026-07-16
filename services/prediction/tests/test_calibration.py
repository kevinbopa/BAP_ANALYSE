from __future__ import annotations

import random
import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.calibration import (
    fit_alpha,
    load_calibration_alpha,
    log_loss_for_alpha,
    save_calibration,
    sharpen,
)


class SharpenTest(unittest.TestCase):
    def test_alpha_one_is_identity(self) -> None:
        self.assertEqual(sharpen(0.5, 0.3, 0.2, 1.0), (0.5, 0.3, 0.2))

    def test_probabilities_stay_normalized(self) -> None:
        for alpha in (0.8, 1.3, 2.0):
            h, d, a = sharpen(0.55, 0.25, 0.20, alpha)
            self.assertAlmostEqual(h + d + a, 1.0, places=9)

    def test_alpha_above_one_increases_favorite(self) -> None:
        h, d, a = sharpen(0.55, 0.25, 0.20, 1.5)
        self.assertGreater(h, 0.55)
        self.assertLess(a, 0.20)

    def test_ranking_is_preserved(self) -> None:
        h, d, a = sharpen(0.50, 0.30, 0.20, 1.8)
        self.assertGreater(h, d)
        self.assertGreater(d, a)


class FitAlphaTest(unittest.TestCase):
    def _synthetic_underconfident(self, n: int = 3000, seed: int = 42):
        """Genere des predictions sous-confiantes : le vrai processus est plus
        tranchant (p^1.5) que ce que le modele annonce."""
        rng = random.Random(seed)
        records = []
        for _ in range(n):
            h = rng.uniform(0.25, 0.65)
            d = rng.uniform(0.15, min(0.35, 0.95 - h))
            a = max(0.02, 1.0 - h - d)
            true_h, true_d, true_a = sharpen(h, d, a, 1.5)
            r = rng.random()
            actual = "HOME" if r < true_h else "DRAW" if r < true_h + true_d else "AWAY"
            records.append((h, d, a, actual))
        return records

    def test_fit_recovers_sharpening_direction(self) -> None:
        records = self._synthetic_underconfident()
        alpha = fit_alpha(records)
        # Le vrai alpha est 1.5 ; l'ajustement doit trouver un affutage > 1.
        self.assertGreater(alpha, 1.2)
        self.assertLess(alpha, 1.9)

    def test_fitted_alpha_improves_log_loss(self) -> None:
        records = self._synthetic_underconfident(seed=7)
        alpha = fit_alpha(records)
        self.assertLess(
            log_loss_for_alpha(records, alpha),
            log_loss_for_alpha(records, 1.0),
        )

    def test_calibrated_data_yields_alpha_near_one(self) -> None:
        rng = random.Random(3)
        records = []
        for _ in range(3000):
            h = rng.uniform(0.25, 0.65)
            d = rng.uniform(0.15, min(0.35, 0.95 - h))
            a = max(0.02, 1.0 - h - d)
            r = rng.random()
            actual = "HOME" if r < h else "DRAW" if r < h + d else "AWAY"
            records.append((h, d, a, actual))
        alpha = fit_alpha(records)
        self.assertGreater(alpha, 0.85)
        self.assertLess(alpha, 1.20)


class PersistenceTest(unittest.TestCase):
    def test_save_and_load_roundtrip(self) -> None:
        from spe_prediction.calibration import load_calibration
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calibration.json"
            save_calibration(1.35, {"fit_n": 1000}, path=path, draw_boost=1.20)
            self.assertEqual(load_calibration(path), (1.35, 1.20))
            self.assertEqual(load_calibration_alpha(path), 1.35)

    def test_missing_file_defaults_to_identity(self) -> None:
        from spe_prediction.calibration import load_calibration
        self.assertEqual(load_calibration(Path("nowhere/cal.json")), (1.0, 1.0))

    def test_absurd_values_are_ignored(self) -> None:
        from spe_prediction.calibration import load_calibration
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calibration.json"
            save_calibration(9.9, {}, path=path)
            self.assertEqual(load_calibration(path), (1.0, 1.0))
            save_calibration(1.3, {}, path=path, draw_boost=5.0)
            self.assertEqual(load_calibration(path), (1.0, 1.0))


class TwoParameterCalibrationTest(unittest.TestCase):
    def test_draw_boost_raises_draw_probability(self) -> None:
        h1, d1, a1 = sharpen(0.50, 0.25, 0.25, 1.0, 1.3)
        self.assertGreater(d1, 0.25)
        self.assertAlmostEqual(h1 + d1 + a1, 1.0, places=9)

    def test_fit_recovers_draw_suppression(self) -> None:
        """Le vrai processus a PLUS de nuls que le modele n'annonce :
        le fit 2D doit trouver draw_boost > 1."""
        import random
        from spe_prediction.calibration import fit_calibration
        rng = random.Random(11)
        records = []
        for _ in range(4000):
            h = rng.uniform(0.30, 0.60)
            d = rng.uniform(0.12, 0.22)          # modele : nul sous-pondere
            a = max(0.02, 1.0 - h - d)
            # realite : nul plus frequent (boost 1.35 renormalise)
            th, td, ta = sharpen(h, d, a, 1.0, 1.35)
            r = rng.random()
            actual = "HOME" if r < th else "DRAW" if r < th + td else "AWAY"
            records.append((h, d, a, actual))
        alpha, beta = fit_calibration(records)
        self.assertGreater(beta, 1.15)
        self.assertLess(beta, 1.60)

    def test_engine_uses_draw_boost_from_file(self) -> None:
        from spe_prediction.domain import FixtureFeatures, TeamStrengthSnapshot
        from spe_prediction.engine import MatchAnalysisEngine
        fixture = FixtureFeatures(
            fixture_id=1,
            home_team=TeamStrengthSnapshot(1, "A", 1700, 0.35, 0.22, played_matches=25, data_quality=0.8),
            away_team=TeamStrengthSnapshot(2, "B", 1640, 0.25, 0.26, played_matches=25, data_quality=0.8),
        )
        base = MatchAnalysisEngine(calibration_alpha=1.0).analyze_fixture(fixture, None)
        boosted_engine = MatchAnalysisEngine(calibration_alpha=1.0)
        boosted_engine._calibration_draw_boost = 1.4
        boosted = boosted_engine.analyze_fixture(fixture, None)
        self.assertGreater(boosted.probabilities.draw, base.probabilities.draw)


class EngineIntegrationTest(unittest.TestCase):
    def test_engine_sharpens_model_component(self) -> None:
        from spe_prediction.domain import FixtureFeatures, TeamStrengthSnapshot
        from spe_prediction.engine import MatchAnalysisEngine

        fixture = FixtureFeatures(
            fixture_id=1,
            home_team=TeamStrengthSnapshot(1, "A", 1750, 0.45, 0.20, recent_form=0.3, played_matches=30, data_quality=0.9),
            away_team=TeamStrengthSnapshot(2, "B", 1520, 0.10, 0.35, recent_form=-0.2, played_matches=30, data_quality=0.9),
        )
        raw = MatchAnalysisEngine(calibration_alpha=1.0).analyze_fixture(fixture, None)
        sharp = MatchAnalysisEngine(calibration_alpha=1.4).analyze_fixture(fixture, None)
        # Le favori doit sortir renforce par la calibration.
        self.assertGreater(sharp.probabilities.home, raw.probabilities.home)
        self.assertAlmostEqual(
            sharp.probabilities.home + sharp.probabilities.draw + sharp.probabilities.away,
            1.0, places=6,
        )


if __name__ == "__main__":
    unittest.main()
