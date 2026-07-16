"""Correlation-detection engine.

Mines the historical database for team- and player-level correlations:

  - PLAYER_PRESENCE : team performance with vs without a given player in the
    lineup ("France avec Mbappe: 2.3 buts/match — sans: 1.4")
  - SYNERGY_PAIR    : recurring scorer-assist duos from the match timeline
  - REST_IMPACT     : short rest (< 4 days) vs normal rest performance
  - H2H             : direct head-to-head record between two teams

Statistical discipline — the three barriers every insight must pass before
it is allowed to influence the engine (is_validated=True):

  1. Minimum sample sizes on BOTH sides of the split.
  2. Two-sided statistical test (Welch approximation, normal CDF via erf —
     no scipy dependency).
  3. Benjamini-Hochberg FDR correction across ALL candidates of a scan:
     searching hundreds of correlations at p<0.05 would otherwise
     "discover" ~5% pure noise.

Non-validated insights are still stored and displayed with their sample
sizes ("indicatif"), but never touch the probabilities.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from typing import Any, Sequence

# --- Barrieres statistiques -------------------------------------------------
MIN_SAMPLE_WITH = 8       # matchs minimum dans la condition
MIN_SAMPLE_WITHOUT = 5    # matchs minimum hors condition
FDR_THRESHOLD = 0.10      # q-value max pour validation (10% de faux positifs toleres)
MIN_EFFECT_POINTS = 0.25  # ecart minimal de points/match pour etre interessant
MIN_EFFECT_GOALS = 0.35   # ecart minimal de buts/match
MIN_SYNERGY_GOALS = 4     # buts minimum pour une paire buteur-passeur
SHORT_REST_DAYS = 4.0


@dataclass
class CandidateInsight:
    insight_code: str
    team_id: int
    subject_label: str
    metric_code: str
    effect_value: float
    baseline_value: float | None
    sample_with: int
    sample_without: int | None
    p_value: float | None
    player_id: int | None = None
    second_player_id: int | None = None
    opponent_team_id: int | None = None
    details: dict[str, Any] = field(default_factory=dict)
    q_value: float | None = None
    is_validated: bool = False


# ============================================================================
# Outils statistiques (sans dependance externe)
# ============================================================================

def _normal_sf(z: float) -> float:
    """Survival function de la normale standard (1 - CDF)."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def welch_p_value(
    mean_a: float, var_a: float, n_a: int,
    mean_b: float, var_b: float, n_b: int,
) -> float | None:
    """p-value bilaterale approchee (Welch, CDF normale).

    Approximation raisonnable pour n >= 5 de chaque cote — coherent avec nos
    barrieres d'echantillon minimal.
    """
    if n_a < 2 or n_b < 2:
        return None
    se_sq = (var_a / n_a) + (var_b / n_b)
    if se_sq <= 0:
        return 1.0
    z = abs(mean_a - mean_b) / math.sqrt(se_sq)
    return max(1e-12, 2.0 * _normal_sf(z))


def benjamini_hochberg(candidates: Sequence[CandidateInsight]) -> None:
    """Assigne q_value a chaque candidat (correction FDR, in place).

    Les candidats sans p-value (descriptifs: SYNERGY_PAIR, H2H) ne participent
    pas a la correction et ne peuvent etre valides statistiquement.
    """
    testable = [c for c in candidates if c.p_value is not None]
    if not testable:
        return
    testable.sort(key=lambda c: c.p_value)  # type: ignore[arg-type]
    n = len(testable)
    # q brut puis monotonie decroissante depuis la fin
    raw = [(c.p_value * n) / (rank + 1) for rank, c in enumerate(testable)]  # type: ignore[operator]
    running_min = 1.0
    for i in range(n - 1, -1, -1):
        running_min = min(running_min, raw[i])
        testable[i].q_value = round(min(1.0, running_min), 6)


def proportion_p_value(successes: int, n: int, p0: float) -> float | None:
    """p-value bilaterale d'une proportion observee vs une reference p0
    (z-test). Utilisee pour les profils de buts (over 2.5, BTTS) compares
    a la moyenne globale du corpus."""
    if n < 5 or not (0.0 < p0 < 1.0):
        return None
    p_hat = successes / n
    se = math.sqrt(p0 * (1.0 - p0) / n)
    if se <= 0:
        return 1.0
    z = abs(p_hat - p0) / se
    return max(1e-12, 2.0 * _normal_sf(z))


def _mean_var(values: Sequence[float]) -> tuple[float, float]:
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, var


# ============================================================================
# Constructions de candidats (purs — testables sans base)
# ============================================================================

def build_presence_candidate(
    team_id: int,
    team_name: str,
    player_id: int,
    player_name: str,
    metric_code: str,
    values_with: Sequence[float],
    values_without: Sequence[float],
) -> CandidateInsight | None:
    """Candidat presence/absence si les echantillons suffisent."""
    n_with, n_without = len(values_with), len(values_without)
    if n_with < MIN_SAMPLE_WITH or n_without < MIN_SAMPLE_WITHOUT:
        return None
    mean_with, var_with = _mean_var(values_with)
    mean_without, var_without = _mean_var(values_without)
    p = welch_p_value(mean_with, var_with, n_with, mean_without, var_without, n_without)
    return CandidateInsight(
        insight_code="PLAYER_PRESENCE",
        team_id=team_id,
        player_id=player_id,
        subject_label=f"{team_name} avec {player_name}",
        metric_code=metric_code,
        effect_value=round(mean_with, 4),
        baseline_value=round(mean_without, 4),
        sample_with=n_with,
        sample_without=n_without,
        p_value=round(p, 6) if p is not None else None,
        details={"player_name": player_name, "team_name": team_name},
    )


COMPETITION_FAMILIES = {
    "WORLD_CUP": ("world cup",),
    # Coupes continentales DE CLUBS — testees avant CONTINENTAL car
    # "europa league" contient "euro" (mot-cle des tournois de selections).
    "CONTINENTAL_CLUB": ("champions league", "europa league", "conference league",
                         "copa libertadores", "copa sudamericana"),
    "CONTINENTAL": ("euro", "european championship", "copa america", "africa cup",
                    "african cup", "asian cup", "gold cup", "confederations cup", "arab cup"),
    "NATIONS_LEAGUE": ("nations league",),
    "QUALIFIER": ("qualif", "qualifying", "wcq"),
    "FRIENDLY": ("friendl",),
}


def competition_family(league_name: str) -> str:
    lowered = (league_name or "").lower()
    # QUALIFIER teste avant WORLD_CUP: "World Cup Qualifying" doit tomber en QUALIFIER.
    for family in ("QUALIFIER", "NATIONS_LEAGUE", "WORLD_CUP", "CONTINENTAL_CLUB",
                   "CONTINENTAL", "FRIENDLY"):
        if any(kw in lowered for kw in COMPETITION_FAMILIES[family]):
            return family
    return "DOMESTIC"


FAMILY_LABELS_FR = {
    "WORLD_CUP": "en Coupe du Monde",
    "CONTINENTAL_CLUB": "en coupe d'Europe",
    "CONTINENTAL": "en tournoi continental",
    "NATIONS_LEAGUE": "en Nations League",
    "QUALIFIER": "en qualifications",
    "FRIENDLY": "en amical",
    "DOMESTIC": "en championnat",
}


def build_split_candidate(
    insight_code: str,
    team_id: int,
    subject_label: str,
    values_in: Sequence[float],
    values_out: Sequence[float],
    metric_code: str = "points_per_match",
    details: dict[str, Any] | None = None,
) -> CandidateInsight | None:
    """Candidat generique 'condition vs hors condition' (COMP_SPLIT,
    TIER_SPLIT, STREAK...)."""
    n_in, n_out = len(values_in), len(values_out)
    if n_in < MIN_SAMPLE_WITH or n_out < MIN_SAMPLE_WITHOUT:
        return None
    mean_in, var_in = _mean_var(values_in)
    mean_out, var_out = _mean_var(values_out)
    p = welch_p_value(mean_in, var_in, n_in, mean_out, var_out, n_out)
    return CandidateInsight(
        insight_code=insight_code,
        team_id=team_id,
        subject_label=subject_label,
        metric_code=metric_code,
        effect_value=round(mean_in, 4),
        baseline_value=round(mean_out, 4),
        sample_with=n_in,
        sample_without=n_out,
        p_value=round(p, 6) if p is not None else None,
        details=details or {},
    )


def build_scoring_candidate(
    team_id: int,
    subject_label: str,
    successes: int,
    n: int,
    global_rate: float,
    metric_code: str,
    details: dict[str, Any] | None = None,
) -> CandidateInsight | None:
    """Candidat proportion vs moyenne globale (over 2.5, BTTS)."""
    if n < MIN_SAMPLE_WITH:
        return None
    p = proportion_p_value(successes, n, global_rate)
    return CandidateInsight(
        insight_code="SCORING_PATTERN",
        team_id=team_id,
        subject_label=subject_label,
        metric_code=metric_code,
        effect_value=round(successes / n, 4),
        baseline_value=round(global_rate, 4),
        sample_with=n,
        sample_without=None,
        p_value=round(p, 6) if p is not None else None,
        details=details or {},
    )


def build_rest_candidate(
    team_id: int,
    team_name: str,
    points_short_rest: Sequence[float],
    points_normal_rest: Sequence[float],
) -> CandidateInsight | None:
    n_short, n_normal = len(points_short_rest), len(points_normal_rest)
    if n_short < MIN_SAMPLE_WITH or n_normal < MIN_SAMPLE_WITHOUT:
        return None
    mean_s, var_s = _mean_var(points_short_rest)
    mean_n, var_n = _mean_var(points_normal_rest)
    p = welch_p_value(mean_s, var_s, n_short, mean_n, var_n, n_normal)
    return CandidateInsight(
        insight_code="REST_IMPACT",
        team_id=team_id,
        subject_label=f"{team_name} avec moins de {int(SHORT_REST_DAYS)} jours de repos",
        metric_code="points_per_match",
        effect_value=round(mean_s, 4),
        baseline_value=round(mean_n, 4),
        sample_with=n_short,
        sample_without=n_normal,
        p_value=round(p, 6) if p is not None else None,
        details={"team_name": team_name},
    )


def validate_candidates(candidates: list[CandidateInsight]) -> list[CandidateInsight]:
    """Applique FDR + seuils d'effet. Retourne la liste (mutee) complete."""
    benjamini_hochberg(candidates)
    for c in candidates:
        if c.q_value is None or c.baseline_value is None:
            c.is_validated = False
            continue
        effect_gap = abs(c.effect_value - float(c.baseline_value))
        if c.metric_code.endswith("_rate"):
            min_effect = 0.15  # ecart de proportion (ex: 15 pts de % sur over 2.5)
        elif "goals" in c.metric_code:
            min_effect = MIN_EFFECT_GOALS
        else:
            min_effect = MIN_EFFECT_POINTS
        c.is_validated = bool(c.q_value <= FDR_THRESHOLD and effect_gap >= min_effect)
    return candidates


# ============================================================================
# Scan complet contre la base
# ============================================================================

class CorrelationScanner:
    def __init__(self, connection) -> None:
        self._connection = connection

    def scan_all(self) -> dict[str, int]:
        candidates: list[CandidateInsight] = []
        with self._connection.cursor() as cursor:
            candidates += self._scan_player_presence(cursor)
            candidates += self._scan_rest_impact(cursor)
            candidates += self._scan_comp_splits(cursor)
            candidates += self._scan_tier_splits(cursor)
            candidates += self._scan_streaks(cursor)
            candidates += self._scan_scoring_patterns(cursor)
        validate_candidates(candidates)

        descriptive: list[CandidateInsight] = []
        with self._connection.cursor() as cursor:
            descriptive += self._scan_synergy_pairs(cursor)

        self._store(candidates + descriptive)
        validated = sum(1 for c in candidates if c.is_validated)
        return {
            "candidates_tested": len(candidates),
            "validated": validated,
            "descriptive": len(descriptive),
        }

    # ------------------------------------------------------------------
    # PLAYER_PRESENCE : points et buts avec/sans chaque joueur
    # ------------------------------------------------------------------
    def _scan_player_presence(self, cursor) -> list[CandidateInsight]:
        # Un seul SQL set-based : pour chaque (equipe, joueur du roster),
        # la liste des resultats d'equipe avec/sans le joueur dans la compo.
        # Limite aux equipes ayant au moins 13 matchs avec compos connues.
        cursor.execute(
            """
            WITH team_matches AS (
                SELECT
                    t.team_id,
                    t.team_name,
                    f.fixture_id,
                    CASE WHEN f.home_team_id = t.team_id THEN fs.home_score ELSE fs.away_score END AS goals_for,
                    CASE
                        WHEN fs.home_score = fs.away_score THEN 1.0
                        WHEN (f.home_team_id = t.team_id) = (fs.home_score > fs.away_score) THEN 3.0
                        ELSE 0.0
                    END AS points
                FROM core.teams t
                JOIN core.fixtures f
                  ON f.home_team_id = t.team_id OR f.away_team_id = t.team_id
                JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
                WHERE fs.home_score IS NOT NULL
                  -- seulement les matchs dont on CONNAIT la compo
                  AND EXISTS (
                      SELECT 1 FROM core.fixture_lineups fl
                      WHERE fl.fixture_id = f.fixture_id AND fl.team_id = t.team_id
                  )
            ),
            eligible_teams AS (
                SELECT team_id FROM team_matches GROUP BY team_id HAVING COUNT(*) >= 13
            )
            SELECT
                tm.team_id,
                tm.team_name,
                p.player_id,
                p.player_name,
                (fl.player_id IS NOT NULL) AS played,
                ARRAY_AGG(tm.points) AS points_arr,
                ARRAY_AGG(tm.goals_for) AS goals_arr
            FROM team_matches tm
            JOIN eligible_teams et ON et.team_id = tm.team_id
            JOIN core.team_squad_members sm ON sm.team_id = tm.team_id
            JOIN core.players p ON p.player_id = sm.player_id
            LEFT JOIN core.fixture_lineups fl
              ON fl.fixture_id = tm.fixture_id AND fl.player_id = p.player_id
            GROUP BY tm.team_id, tm.team_name, p.player_id, p.player_name, (fl.player_id IS NOT NULL)
            """
        )
        # Regrouper les deux cotes (played true/false) par (team, player)
        sides: dict[tuple[int, int], dict[str, Any]] = {}
        for team_id, team_name, player_id, player_name, played, points_arr, goals_arr in cursor.fetchall():
            key = (int(team_id), int(player_id))
            entry = sides.setdefault(key, {"team_name": team_name, "player_name": player_name})
            entry["with" if played else "without"] = (
                [float(x) for x in points_arr],
                [float(x) for x in goals_arr],
            )

        candidates: list[CandidateInsight] = []
        for (team_id, player_id), entry in sides.items():
            if "with" not in entry or "without" not in entry:
                continue
            pts_with, goals_with = entry["with"]
            pts_without, goals_without = entry["without"]
            for metric, vw, vo in (
                ("points_per_match", pts_with, pts_without),
                ("goals_per_match", goals_with, goals_without),
            ):
                candidate = build_presence_candidate(
                    team_id, str(entry["team_name"]), player_id, str(entry["player_name"]),
                    metric, vw, vo,
                )
                if candidate:
                    candidates.append(candidate)
        return candidates

    # ------------------------------------------------------------------
    # REST_IMPACT : repos court vs normal
    # ------------------------------------------------------------------
    def _scan_rest_impact(self, cursor) -> list[CandidateInsight]:
        cursor.execute(
            """
            WITH team_matches AS (
                SELECT
                    t.team_id,
                    t.team_name,
                    f.kickoff_utc,
                    EXTRACT(EPOCH FROM (
                        f.kickoff_utc - LAG(f.kickoff_utc) OVER (
                            PARTITION BY t.team_id ORDER BY f.kickoff_utc
                        )
                    )) / 86400.0 AS rest_days,
                    CASE
                        WHEN fs.home_score = fs.away_score THEN 1.0
                        WHEN (f.home_team_id = t.team_id) = (fs.home_score > fs.away_score) THEN 3.0
                        ELSE 0.0
                    END AS points
                FROM core.teams t
                JOIN core.fixtures f
                  ON f.home_team_id = t.team_id OR f.away_team_id = t.team_id
                JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
                WHERE fs.home_score IS NOT NULL AND f.kickoff_utc IS NOT NULL
            )
            SELECT team_id, team_name,
                   (rest_days < %(short)s) AS is_short,
                   ARRAY_AGG(points) AS points_arr
            FROM team_matches
            WHERE rest_days IS NOT NULL AND rest_days BETWEEN 1 AND 60
            GROUP BY team_id, team_name, (rest_days < %(short)s)
            """,
            {"short": SHORT_REST_DAYS},
        )
        sides: dict[int, dict[str, Any]] = {}
        for team_id, team_name, is_short, points_arr in cursor.fetchall():
            entry = sides.setdefault(int(team_id), {"team_name": team_name})
            entry["short" if is_short else "normal"] = [float(x) for x in points_arr]

        candidates: list[CandidateInsight] = []
        for team_id, entry in sides.items():
            if "short" not in entry or "normal" not in entry:
                continue
            candidate = build_rest_candidate(
                team_id, str(entry["team_name"]), entry["short"], entry["normal"]
            )
            if candidate:
                candidates.append(candidate)
        return candidates

    # ------------------------------------------------------------------
    # Base commune des splits : matchs + points + contexte par equipe
    # ------------------------------------------------------------------
    def _fetch_team_match_rows(self, cursor) -> list[tuple]:
        """(team_id, team_name, kickoff, league_name, points, gf, ga,
        opponent_id) pour tous les matchs termines — un seul fetch pour
        toutes les familles de split."""
        cursor.execute(
            """
            SELECT
                t.team_id, t.team_name, f.kickoff_utc, l.league_name,
                CASE
                    WHEN fs.home_score = fs.away_score THEN 1.0
                    WHEN (f.home_team_id = t.team_id) = (fs.home_score > fs.away_score) THEN 3.0
                    ELSE 0.0
                END AS points,
                CASE WHEN f.home_team_id = t.team_id THEN fs.home_score ELSE fs.away_score END AS gf,
                CASE WHEN f.home_team_id = t.team_id THEN fs.away_score ELSE fs.home_score END AS ga,
                CASE WHEN f.home_team_id = t.team_id THEN f.away_team_id ELSE f.home_team_id END AS opponent_id
            FROM core.teams t
            JOIN core.fixtures f ON f.home_team_id = t.team_id OR f.away_team_id = t.team_id
            JOIN core.leagues l ON l.league_id = f.league_id
            JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
            WHERE fs.home_score IS NOT NULL AND f.kickoff_utc IS NOT NULL
            ORDER BY t.team_id, f.kickoff_utc
            """
        )
        return cursor.fetchall()

    def _team_rows_grouped(self, cursor) -> dict[int, list[tuple]]:
        grouped: dict[int, list[tuple]] = {}
        for row in self._fetch_team_match_rows(cursor):
            grouped.setdefault(int(row[0]), []).append(row)
        return grouped

    # ------------------------------------------------------------------
    # COMP_SPLIT : performance par type de competition
    # ------------------------------------------------------------------
    def _scan_comp_splits(self, cursor) -> list[CandidateInsight]:
        candidates: list[CandidateInsight] = []
        for team_id, rows in self._team_rows_grouped(cursor).items():
            team_name = str(rows[0][1])
            by_family: dict[str, list[float]] = {}
            for _tid, _name, _kick, league, points, _gf, _ga, _opp in rows:
                by_family.setdefault(competition_family(str(league)), []).append(float(points))
            all_points = [p for pts in by_family.values() for p in pts]
            for family, pts_in in by_family.items():
                pts_out = [p for f, pts in by_family.items() if f != family for p in pts]
                if not pts_out or len(all_points) < 15:
                    continue
                candidate = build_split_candidate(
                    "COMP_SPLIT", team_id,
                    f"{team_name} {FAMILY_LABELS_FR.get(family, family)}",
                    pts_in, pts_out,
                    details={"family": family, "team_name": team_name},
                )
                if candidate:
                    candidates.append(candidate)
        return candidates

    # ------------------------------------------------------------------
    # TIER_SPLIT : performance contre adversaires forts (Elo 1700+)
    # ------------------------------------------------------------------
    STRONG_OPPONENT_ELO = 1700.0

    def _scan_tier_splits(self, cursor) -> list[CandidateInsight]:
        from spe_prediction.rating import compute_elo_ratings
        cursor.execute(
            """
            SELECT f.kickoff_utc, f.home_team_id, f.away_team_id,
                   fs.home_score, fs.away_score, l.league_name
            FROM core.fixtures f
            JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
            JOIN core.leagues l ON l.league_id = f.league_id
            WHERE fs.home_score IS NOT NULL AND f.kickoff_utc IS NOT NULL
            ORDER BY f.kickoff_utc
            """
        )
        ratings = compute_elo_ratings(cursor.fetchall())

        candidates: list[CandidateInsight] = []
        for team_id, rows in self._team_rows_grouped(cursor).items():
            team_name = str(rows[0][1])
            vs_strong: list[float] = []
            vs_rest: list[float] = []
            for _tid, _name, _kick, _league, points, _gf, _ga, opponent_id in rows:
                # Approximation assumee: tier de l'adversaire = son Elo ACTUEL
                # (pas celui de l'epoque). Documente dans details.
                if ratings.get(int(opponent_id), 1500.0) >= self.STRONG_OPPONENT_ELO:
                    vs_strong.append(float(points))
                else:
                    vs_rest.append(float(points))
            candidate = build_split_candidate(
                "TIER_SPLIT", team_id,
                f"{team_name} contre adversaires forts (Elo {int(self.STRONG_OPPONENT_ELO)}+)",
                vs_strong, vs_rest,
                details={"team_name": team_name, "tier_method": "current_elo_approx"},
            )
            if candidate:
                candidates.append(candidate)
        return candidates

    # ------------------------------------------------------------------
    # STREAK : performance apres une defaite par 2+ buts
    # ------------------------------------------------------------------
    def _scan_streaks(self, cursor) -> list[CandidateInsight]:
        candidates: list[CandidateInsight] = []
        for team_id, rows in self._team_rows_grouped(cursor).items():
            team_name = str(rows[0][1])
            after_big_loss: list[float] = []
            after_other: list[float] = []
            prev_margin: int | None = None
            for _tid, _name, _kick, _league, points, gf, ga, _opp in rows:
                if prev_margin is not None:
                    if prev_margin <= -2:
                        after_big_loss.append(float(points))
                    else:
                        after_other.append(float(points))
                prev_margin = int(gf) - int(ga)
            candidate = build_split_candidate(
                "STREAK", team_id,
                f"{team_name} apres une defaite par 2+ buts",
                after_big_loss, after_other,
                details={"team_name": team_name, "condition": "after_big_loss"},
            )
            if candidate:
                candidates.append(candidate)
        return candidates

    # ------------------------------------------------------------------
    # SCORING_PATTERN : over 2.5 et BTTS vs moyenne globale
    # ------------------------------------------------------------------
    def _scan_scoring_patterns(self, cursor) -> list[CandidateInsight]:
        grouped = self._team_rows_grouped(cursor)
        total_matches = 0
        total_over = 0
        total_btts = 0
        for rows in grouped.values():
            for _tid, _name, _kick, _league, _pts, gf, ga, _opp in rows:
                total_matches += 1
                if int(gf) + int(ga) >= 3:
                    total_over += 1
                if int(gf) > 0 and int(ga) > 0:
                    total_btts += 1
        if total_matches < 100:
            return []
        global_over = total_over / total_matches
        global_btts = total_btts / total_matches

        candidates: list[CandidateInsight] = []
        for team_id, rows in grouped.items():
            team_name = str(rows[0][1])
            n = len(rows)
            over = sum(1 for r in rows if int(r[5]) + int(r[6]) >= 3)
            btts = sum(1 for r in rows if int(r[5]) > 0 and int(r[6]) > 0)
            for label, successes, rate_name, global_rate in (
                (f"{team_name}: matchs a 3 buts ou plus (over 2.5)", over, "over25_rate", global_over),
                (f"{team_name}: les deux equipes marquent (BTTS)", btts, "btts_rate", global_btts),
            ):
                candidate = build_scoring_candidate(
                    team_id, label, successes, n, global_rate, rate_name,
                    details={"team_name": team_name},
                )
                if candidate:
                    candidates.append(candidate)
        return candidates

    # ------------------------------------------------------------------
    # SYNERGY_PAIR : paires buteur-passeur (descriptif, pas de p-value)
    # ------------------------------------------------------------------
    def _scan_synergy_pairs(self, cursor) -> list[CandidateInsight]:
        cursor.execute(
            """
            SELECT
                COALESCE(fl.team_id, 0) AS team_id,
                COALESCE(t.team_name, '?') AS team_name,
                ft.player_id, ps.player_name,
                ft.assist_player_id, pa.player_name AS assist_name,
                COUNT(*) AS goals_together
            FROM core.fixture_timeline ft
            JOIN core.players ps ON ps.player_id = ft.player_id
            JOIN core.players pa ON pa.player_id = ft.assist_player_id
            LEFT JOIN core.fixture_lineups fl
              ON fl.fixture_id = ft.fixture_id AND fl.player_id = ft.player_id
            LEFT JOIN core.teams t ON t.team_id = fl.team_id
            WHERE ft.event_code = 'GOAL'
              AND ft.player_id IS NOT NULL
              AND ft.assist_player_id IS NOT NULL
            GROUP BY 1, 2, 3, 4, 5, 6
            HAVING COUNT(*) >= %(min_goals)s
            ORDER BY goals_together DESC
            LIMIT 500
            """,
            {"min_goals": MIN_SYNERGY_GOALS},
        )
        results: list[CandidateInsight] = []
        for team_id, team_name, scorer_id, scorer, assist_id, assister, goals in cursor.fetchall():
            if not team_id:
                continue
            results.append(CandidateInsight(
                insight_code="SYNERGY_PAIR",
                team_id=int(team_id),
                player_id=int(scorer_id),
                second_player_id=int(assist_id),
                subject_label=f"{scorer} servi par {assister} ({team_name})",
                metric_code="goal_contributions",
                effect_value=float(goals),
                baseline_value=None,
                sample_with=int(goals),
                sample_without=None,
                p_value=None,
                details={"scorer": scorer, "assister": assister, "team_name": team_name},
            ))
        return results

    # ------------------------------------------------------------------
    # Stockage (remplacement complet a chaque scan)
    # ------------------------------------------------------------------
    def _store(self, candidates: list[CandidateInsight]) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute("DELETE FROM model.correlation_insights")
            for c in candidates:
                cursor.execute(
                    """
                    INSERT INTO model.correlation_insights (
                        insight_code, team_id, player_id, second_player_id,
                        opponent_team_id, subject_label, metric_code,
                        effect_value, baseline_value, sample_with, sample_without,
                        p_value, q_value, is_validated, details_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        c.insight_code, c.team_id, c.player_id, c.second_player_id,
                        c.opponent_team_id, c.subject_label, c.metric_code,
                        c.effect_value, c.baseline_value, c.sample_with, c.sample_without,
                        c.p_value, c.q_value, c.is_validated, json.dumps(c.details),
                    ),
                )
        self._connection.commit()


# ============================================================================
# Liaison au moteur de prediction : insights -> ajustements + textes
# ============================================================================

# Malus xG conservateur quand une correlation REST_IMPACT validee s'applique
# au match courant. Volontairement plafonne : la correlation est un signal
# supplementaire, pas un remplacement du modele.
REST_ADJUST_GOAL_DELTA = 0.10
MAX_INSIGHT_TEXTS = 5


# Ajustement generique quand un split contextuel valide s'applique au match
# courant (COMP_SPLIT / TIER_SPLIT / STREAK). Plafonne et signe par l'effet.
SPLIT_ADJUST_GOAL_DELTA = 0.08


def fixture_insight_signals(
    insight_rows: Sequence[tuple],
    home_team_id: int,
    away_team_id: int,
    home_rest_days: float | None,
    away_rest_days: float | None,
    home_context: dict | None = None,
    away_context: dict | None = None,
) -> tuple[tuple[str, ...], dict[str, float]]:
    """Convertit les insights des deux equipes en (textes, ajustements xG).

    ``insight_rows``: (insight_code, team_id, subject_label, effect_value,
    baseline_value, sample_with, sample_without, is_validated[, details_json]).

    ``home_context``/``away_context`` (optionnels) portent les conditions du
    match courant : {"comp_family": str, "after_big_loss": bool,
    "opponent_is_strong": bool}.

    Regles :
      - Seuls les insights VALIDES produisent un ajustement numerique.
      - Un split contextuel ne s'applique que si SA condition est remplie
        au match courant (famille de competition, adversaire fort, apres
        grosse defaite, repos court).
      - Les descriptifs (SYNERGY_PAIR, SCORING_PATTERN) alimentent le texte.
    """
    texts: list[str] = []
    adjustments: dict[str, float] = {}
    home_context = home_context or {}
    away_context = away_context or {}

    def _apply_split(side: str, effect: float, baseline: float, label: str, n_w, n_wo, condition_label: str) -> None:
        sign = 1.0 if effect > baseline else -1.0
        key = f"{side}_goal_delta"
        adjustments[key] = adjustments.get(key, 0.0) + sign * SPLIT_ADJUST_GOAL_DELTA
        texts.append(
            f"Correlation validee: {label} = {effect:.2f} pts/match "
            f"(vs {baseline:.2f} sinon, {n_w}/{n_wo} matchs) - {condition_label}: ajustement applique"
        )

    for row in insight_rows:
        code, team_id, label, effect, baseline, n_with, n_without, validated = row[:8]
        details = row[8] if len(row) > 8 and isinstance(row[8], dict) else {}
        team_id = int(team_id)
        if team_id == home_team_id:
            side, rest, ctx = "home", home_rest_days, home_context
        elif team_id == away_team_id:
            side, rest, ctx = "away", away_rest_days, away_context
        else:
            continue

        effect_f = float(effect)
        baseline_f = float(baseline) if baseline is not None else None

        if code == "REST_IMPACT" and validated and baseline_f is not None:
            if rest is not None and rest < SHORT_REST_DAYS:
                key = f"{side}_goal_delta"
                adjustments[key] = adjustments.get(key, 0.0) - REST_ADJUST_GOAL_DELTA
                texts.append(
                    f"Correlation validee: {label} = {effect_f:.2f} pts/match "
                    f"(vs {baseline_f:.2f} normalement, {n_with}/{n_without} matchs) "
                    f"- ET le repos actuel est court: malus applique"
                )
        elif code == "COMP_SPLIT" and validated and baseline_f is not None:
            if details.get("family") and details["family"] == ctx.get("comp_family"):
                _apply_split(side, effect_f, baseline_f, label, n_with, n_without,
                             "ce match est de ce type")
        elif code == "TIER_SPLIT" and validated and baseline_f is not None:
            if ctx.get("opponent_is_strong"):
                _apply_split(side, effect_f, baseline_f, label, n_with, n_without,
                             "l'adversaire du jour est fort")
        elif code == "STREAK" and validated and baseline_f is not None:
            if ctx.get("after_big_loss"):
                _apply_split(side, effect_f, baseline_f, label, n_with, n_without,
                             "l'equipe sort d'une grosse defaite")
        elif code == "PLAYER_PRESENCE" and validated and baseline_f is not None:
            texts.append(
                f"Correlation validee: {label} = {effect_f:.2f} "
                f"(vs {baseline_f:.2f} sans lui, {n_with}/{n_without} matchs)"
            )
        elif code == "SCORING_PATTERN" and validated and baseline_f is not None:
            texts.append(
                f"Profil: {label} = {effect_f:.0%} (moyenne globale {baseline_f:.0%}, "
                f"{n_with} matchs)"
            )
        elif code == "SYNERGY_PAIR":
            texts.append(f"Duo: {label} - {effect_f:.0f} buts ensemble")

    return tuple(texts[:MAX_INSIGHT_TEXTS]), adjustments
