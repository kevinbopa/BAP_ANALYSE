"""Tests du moteur de correlations — fonctions statistiques pures."""
from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spe_prediction.correlations import (
    CandidateInsight,
    benjamini_hochberg,
    build_presence_candidate,
    build_rest_candidate,
    competition_family,
    validate_candidates,
    welch_p_value,
)
from spe_prediction.rating import competition_weight


class CompetitionFamilyTest(unittest.TestCase):
    def test_european_club_cups_have_their_own_family(self) -> None:
        self.assertEqual(competition_family("UEFA Champions League"), "CONTINENTAL_CLUB")
        # "europa" contient "euro" : ne doit PAS tomber en CONTINENTAL (selections).
        self.assertEqual(competition_family("UEFA Europa League"), "CONTINENTAL_CLUB")
        self.assertEqual(competition_family("UEFA Conference League"), "CONTINENTAL_CLUB")

    def test_selections_tournaments_unchanged(self) -> None:
        self.assertEqual(competition_family("UEFA European Championships"), "CONTINENTAL")
        self.assertEqual(competition_family("FIFA World Cup"), "WORLD_CUP")
        self.assertEqual(competition_family("World Cup Qualifying UEFA"), "QUALIFIER")

    def test_european_domestic_leagues_are_domestic(self) -> None:
        for league in ("Dutch Eredivisie", "Portuguese Primeira Liga",
                       "Turkish Super Lig", "Norwegian Eliteserien"):
            self.assertEqual(competition_family(league), "DOMESTIC")

    def test_club_cup_weights(self) -> None:
        self.assertEqual(competition_weight("UEFA Champions League"), 1.18)
        self.assertEqual(competition_weight("UEFA Europa League"), 1.10)
        self.assertEqual(competition_weight("UEFA Conference League"), 1.10)
        # Un championnat domestique reste au poids neutre.
        self.assertEqual(competition_weight("Swedish Allsvenskan"), 1.0)
        # La Coupe du monde reste au-dessus de tout.
        self.assertGreater(competition_weight("FIFA World Cup"), 1.18)


class WelchTest(unittest.TestCase):
    def test_identical_groups_yield_high_p(self) -> None:
        p = welch_p_value(1.5, 1.0, 20, 1.5, 1.0, 20)
        self.assertIsNotNone(p)
        self.assertGreater(p, 0.9)

    def test_clearly_different_groups_yield_low_p(self) -> None:
        p = welch_p_value(2.5, 0.5, 30, 0.8, 0.5, 30)
        self.assertIsNotNone(p)
        self.assertLess(p, 0.001)

    def test_tiny_samples_rejected(self) -> None:
        self.assertIsNone(welch_p_value(2.0, 1.0, 1, 1.0, 1.0, 10))


class FDRTest(unittest.TestCase):
    def test_noise_candidates_are_not_validated(self) -> None:
        # 20 candidats avec p-values uniformes (bruit pur) : la correction
        # FDR doit empecher la validation en masse.
        candidates = [
            CandidateInsight(
                insight_code="PLAYER_PRESENCE", team_id=1, subject_label=f"c{i}",
                metric_code="points_per_match", effect_value=2.0, baseline_value=1.0,
                sample_with=10, sample_without=10, p_value=(i + 1) * 0.05,
            )
            for i in range(20)
        ]
        validate_candidates(candidates)
        validated = [c for c in candidates if c.is_validated]
        # p le plus bas = 0.05, q = 0.05*20/1 = 1.0 -> rien ne passe
        self.assertEqual(len(validated), 0)

    def test_strong_signal_survives_fdr(self) -> None:
        candidates = [
            CandidateInsight(
                insight_code="PLAYER_PRESENCE", team_id=1, subject_label="signal",
                metric_code="points_per_match", effect_value=2.4, baseline_value=0.9,
                sample_with=25, sample_without=15, p_value=0.0001,
            )
        ] + [
            CandidateInsight(
                insight_code="PLAYER_PRESENCE", team_id=1, subject_label=f"bruit{i}",
                metric_code="points_per_match", effect_value=1.6, baseline_value=1.5,
                sample_with=10, sample_without=10, p_value=0.4 + i * 0.02,
            )
            for i in range(10)
        ]
        validate_candidates(candidates)
        self.assertTrue(candidates[0].is_validated)
        self.assertFalse(any(c.is_validated for c in candidates[1:]))

    def test_small_effect_rejected_even_if_significant(self) -> None:
        # Significatif statistiquement mais effet minuscule -> pas interessant.
        candidates = [
            CandidateInsight(
                insight_code="PLAYER_PRESENCE", team_id=1, subject_label="micro",
                metric_code="points_per_match", effect_value=1.58, baseline_value=1.50,
                sample_with=500, sample_without=500, p_value=0.001,
            )
        ]
        validate_candidates(candidates)
        self.assertFalse(candidates[0].is_validated)

    def test_descriptive_candidates_never_validated(self) -> None:
        candidates = [
            CandidateInsight(
                insight_code="SYNERGY_PAIR", team_id=1, subject_label="duo",
                metric_code="goal_contributions", effect_value=8.0, baseline_value=None,
                sample_with=8, sample_without=None, p_value=None,
            )
        ]
        validate_candidates(candidates)
        self.assertFalse(candidates[0].is_validated)
        benjamini_hochberg(candidates)  # ne doit pas crasher sans p-values


class FixtureInsightSignalsTest(unittest.TestCase):
    """Liaison insights -> moteur : textes + ajustements."""

    def _rest_row(self, team_id: int, validated: bool = True):
        return ("REST_IMPACT", team_id, "Peru avec moins de 4 jours de repos",
                0.59, 1.56, 22, 59, validated)

    def test_validated_rest_insight_applies_malus_only_when_rest_is_short(self) -> None:
        from spe_prediction.correlations import fixture_insight_signals
        # Repos court -> malus applique
        texts, adj = fixture_insight_signals([self._rest_row(10)], 10, 20, home_rest_days=2.5, away_rest_days=8.0)
        self.assertIn("home_goal_delta", adj)
        self.assertLess(adj["home_goal_delta"], 0)
        self.assertTrue(any("malus applique" in t for t in texts))
        # Repos normal -> aucun ajustement
        texts2, adj2 = fixture_insight_signals([self._rest_row(10)], 10, 20, home_rest_days=9.0, away_rest_days=8.0)
        self.assertEqual(adj2, {})

    def test_non_validated_insight_never_adjusts(self) -> None:
        from spe_prediction.correlations import fixture_insight_signals
        texts, adj = fixture_insight_signals(
            [self._rest_row(10, validated=False)], 10, 20, home_rest_days=2.0, away_rest_days=8.0
        )
        self.assertEqual(adj, {})

    def test_synergy_pair_is_text_only(self) -> None:
        from spe_prediction.correlations import fixture_insight_signals
        rows = [("SYNERGY_PAIR", 20, "Mbappe servi par Griezmann (France)", 7.0, None, 7, None, False)]
        texts, adj = fixture_insight_signals(rows, 10, 20, None, None)
        self.assertEqual(adj, {})
        self.assertTrue(any("Duo" in t for t in texts))

    def test_insights_flow_into_explanation(self) -> None:
        from spe_prediction.domain import FixtureFeatures, MarketOdds, TeamStrengthSnapshot
        from spe_prediction.engine import MatchAnalysisEngine
        engine = MatchAnalysisEngine()
        fixture = FixtureFeatures(
            fixture_id=1,
            home_team=TeamStrengthSnapshot(1, "Peru", 1650, 0.3, 0.25, played_matches=20, data_quality=0.7),
            away_team=TeamStrengthSnapshot(2, "Chili", 1640, 0.28, 0.26, played_matches=20, data_quality=0.7),
            insights=("Correlation validee: Peru avec moins de 4 jours de repos = 0.59 pts/match (vs 1.56 normalement, 22/59 matchs) - ET le repos actuel est court: malus applique",),
        )
        market = MarketOdds(bookmaker="C", home_odd=2.4, draw_odd=3.2, away_odd=3.1, source_count=10)
        result = engine.analyze_fixture(fixture, market)
        self.assertIn("Correlations:", result.explanation)
        self.assertIn("Peru avec moins de 4 jours", result.explanation)


class NewFamiliesTest(unittest.TestCase):
    def test_competition_family_classifier(self) -> None:
        from spe_prediction.correlations import competition_family
        self.assertEqual(competition_family("FIFA World Cup"), "WORLD_CUP")
        self.assertEqual(competition_family("World Cup Qualifying UEFA"), "QUALIFIER")
        self.assertEqual(competition_family("International Friendlies"), "FRIENDLY")
        self.assertEqual(competition_family("UEFA Nations League"), "NATIONS_LEAGUE")
        self.assertEqual(competition_family("Copa America"), "CONTINENTAL")
        self.assertEqual(competition_family("English Premier League"), "DOMESTIC")

    def test_proportion_p_value(self) -> None:
        from spe_prediction.correlations import proportion_p_value
        # 50/100 vs p0=0.5 -> p tres haut ; 80/100 vs 0.5 -> p tres bas
        self.assertGreater(proportion_p_value(50, 100, 0.5), 0.9)
        self.assertLess(proportion_p_value(80, 100, 0.5), 0.001)
        self.assertIsNone(proportion_p_value(2, 3, 0.5))

    def test_split_candidate_builder(self) -> None:
        from spe_prediction.correlations import build_split_candidate
        c = build_split_candidate(
            "COMP_SPLIT", 5, "Peru en amical",
            [3.0] * 10, [1.0] * 10, details={"family": "FRIENDLY"},
        )
        self.assertIsNotNone(c)
        self.assertEqual(c.insight_code, "COMP_SPLIT")
        self.assertEqual(c.details["family"], "FRIENDLY")

    def test_scoring_candidate_rate_threshold(self) -> None:
        from spe_prediction.correlations import build_scoring_candidate, validate_candidates
        # 85% over vs 50% global sur 40 matchs -> gap 0.35, doit passer si q ok
        c = build_scoring_candidate(5, "X over", 34, 40, 0.50, "over25_rate")
        self.assertIsNotNone(c)
        validate_candidates([c])
        self.assertTrue(c.is_validated)
        # gap trop petit (55% vs 50%) -> rejete meme si significatif
        c2 = build_scoring_candidate(5, "Y over", 220, 400, 0.50, "over25_rate")
        validate_candidates([c2])
        self.assertFalse(c2.is_validated)

    def test_contextual_split_signals(self) -> None:
        from spe_prediction.correlations import fixture_insight_signals
        comp_row = ("COMP_SPLIT", 10, "Peru en amical", 2.4, 1.2, 12, 30, True,
                    {"family": "FRIENDLY"})
        # Match amical -> ajustement positif (meilleur en amical)
        texts, adj = fixture_insight_signals(
            [comp_row], 10, 20, None, None,
            home_context={"comp_family": "FRIENDLY"}, away_context={},
        )
        self.assertGreater(adj.get("home_goal_delta", 0), 0)
        # Match de Coupe du Monde -> la condition ne s'applique pas
        _, adj2 = fixture_insight_signals(
            [comp_row], 10, 20, None, None,
            home_context={"comp_family": "WORLD_CUP"}, away_context={},
        )
        self.assertEqual(adj2, {})

    def test_streak_and_tier_conditions(self) -> None:
        from spe_prediction.correlations import fixture_insight_signals
        streak_row = ("STREAK", 20, "Ghana apres une defaite par 2+ buts", 0.4, 1.4, 9, 40, True, {})
        tier_row = ("TIER_SPLIT", 20, "Ghana contre adversaires forts", 0.5, 1.6, 10, 35, True, {})
        texts, adj = fixture_insight_signals(
            [streak_row, tier_row], 10, 20, None, None,
            home_context={},
            away_context={"after_big_loss": True, "opponent_is_strong": True},
        )
        # Les deux malus s'appliquent au cote exterieur (effet < baseline)
        self.assertLess(adj.get("away_goal_delta", 0), -0.10)
        self.assertEqual(len([t for t in texts if "ajustement applique" in t]), 2)


class CandidateBuildersTest(unittest.TestCase):
    def test_presence_candidate_requires_min_samples(self) -> None:
        few_with = [3.0] * 5
        enough_without = [1.0] * 10
        self.assertIsNone(
            build_presence_candidate(1, "France", 9, "Mbappe", "points_per_match", few_with, enough_without)
        )

    def test_presence_candidate_built_with_correct_means(self) -> None:
        with_vals = [3.0, 3.0, 1.0, 3.0, 0.0, 3.0, 3.0, 1.0]      # 8 matchs
        without_vals = [0.0, 1.0, 1.0, 0.0, 3.0]                   # 5 matchs
        c = build_presence_candidate(1, "France", 9, "Mbappe", "points_per_match", with_vals, without_vals)
        self.assertIsNotNone(c)
        self.assertAlmostEqual(c.effect_value, sum(with_vals) / 8, places=3)
        self.assertAlmostEqual(float(c.baseline_value), 1.0, places=3)
        self.assertIn("Mbappe", c.subject_label)

    def test_rest_candidate(self) -> None:
        short = [0.0, 1.0, 0.0, 1.0, 0.0, 3.0, 0.0, 1.0]
        normal = [3.0, 3.0, 1.0, 3.0, 3.0, 1.0]
        c = build_rest_candidate(2, "Suisse", short, normal)
        self.assertIsNotNone(c)
        self.assertEqual(c.insight_code, "REST_IMPACT")
        self.assertLess(c.effect_value, float(c.baseline_value))


if __name__ == "__main__":
    unittest.main()
