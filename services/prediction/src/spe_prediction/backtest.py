"""Backtest walk-forward du moteur analytique (Poisson + Elo ajusté).

Rejoue tout l'historique CHRONOLOGIQUEMENT : pour chaque match, les
probabilités sont calculées uniquement avec ce qui était connu AVANT le
coup d'envoi (ratings Elo au fil de l'eau, forme récente, fatigue), puis
comparées au résultat réel. Zéro fuite temporelle par construction.

Limites assumées (documentées) :
  - Pas de cotes historiques -> on évalue le moteur ANALYTIQUE seul
    (sans ancre marché). Le ROI réel des deals se mesure en avant via
    run_settle_deals.py.
  - XGBoost est exclu (entraîné sur ce même historique = fuite).

Métriques : accuracy du pronostic, log loss, Brier multi-classe,
calibration par tranche de probabilité, ventilation par famille de
compétition.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from typing import Any

from spe_prediction.derived_markets import derived_market_probabilities
from spe_prediction.domain import FixtureFeatures, TeamStrengthSnapshot
from spe_prediction.engine import MatchAnalysisEngine
from spe_prediction.exact_score import build_exact_score_distribution
from spe_prediction.correlations import competition_family
from spe_prediction.rating import (
    BASE_RATING,
    HOME_BONUS_ELO,
    BASE_K,
    _margin_multiplier,
    competition_weight,
    is_neutral_venue,
)

RECENCY_HALF_LIFE_DAYS = 365.0
HISTORY_WINDOW = 80
MIN_PRIOR_MATCHES = 8


@dataclass
class _TeamState:
    rating: float = BASE_RATING
    # deque de (kickoff, league_name, goals_for, goals_against, points)
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_WINDOW))


def _weighted_metrics(history: deque, as_of: datetime) -> dict[str, float]:
    total_w = sum_gf = sum_ga = sum_pts = 0.0
    for kickoff, league, gf, ga, pts in history:
        age_days = max(0.0, (as_of - kickoff).total_seconds() / 86400.0)
        w = math.pow(0.5, age_days / RECENCY_HALF_LIFE_DAYS) * competition_weight(league)
        total_w += w
        sum_gf += gf * w
        sum_ga += ga * w
        sum_pts += pts * w
    if total_w <= 0:
        return {"gf": 1.2, "ga": 1.2, "pts": 0.5, "quality": 0.0}
    return {
        "gf": sum_gf / total_w,
        "ga": sum_ga / total_w,
        "pts": sum_pts / total_w,
        "quality": min(1.0, total_w / 12.0),
    }


def _fatigue(history: deque, as_of: datetime) -> float:
    if not history:
        return 0.0
    last_kick = history[-1][0]
    rest_days = max(0.0, (as_of - last_kick).total_seconds() / 86400.0)
    short_rest = max(0.0, min(1.0, (6.0 - rest_days) / 6.0))
    recent_21d = sum(1 for k, *_ in history if (as_of - k).total_seconds() <= 21 * 86400)
    congestion = max(0.0, min(1.0, recent_21d / 6.0))
    return min(1.0, 0.65 * short_rest + 0.35 * congestion)


def _snapshot(team_id: int, name: str, state: _TeamState, as_of: datetime) -> TeamStrengthSnapshot:
    m = _weighted_metrics(state.history, as_of)
    form = (m["pts"] / 3.0 - 0.5) * 2.0 if state.history else 0.0
    return TeamStrengthSnapshot(
        team_id=team_id,
        team_name=name,
        elo_rating=state.rating,
        attack_rating=max(0.05, m["gf"] - 1.0),
        defense_rating=max(0.05, m["ga"]),
        recent_form=form,
        fatigue_penalty=_fatigue(state.history, as_of),
        played_matches=len(state.history),
        data_quality=m["quality"],
    )


def run_backtest(
    connection,
    evaluate_from: datetime,
    evaluate_to: datetime | None = None,
    calibration_alpha: float | None = 1.0,
    collect_records: bool = False,
    collect_derived: bool = False,
) -> dict[str, Any]:
    """calibration_alpha=1.0 (defaut) = probabilites brutes, necessaires a
    l'ajustement de la calibration. None = charger l'alpha ajuste (evaluation
    du moteur calibre).

    collect_derived : collecte aussi, par match evalue, les probabilites
    BRUTES over 2.5 / BTTS issues de la matrice de scores (meme chaine que la
    prod : build_exact_score_distribution alignee sur le 1X2 du moteur) et
    l'issue reelle — la matiere premiere de la calibration derivee."""
    evaluate_to = evaluate_to or datetime.now(timezone.utc)
    # Analytique pur : pas de XGBoost (fuite), pas de marche (pas de cotes historiques).
    engine = MatchAnalysisEngine(calibration_alpha=calibration_alpha)
    records: list[tuple] = []
    derived_records: list[tuple] = []

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT f.kickoff_utc, f.home_team_id, ht.team_name,
                   f.away_team_id, at.team_name,
                   fs.home_score, fs.away_score, l.league_name
            FROM core.fixtures f
            JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
            JOIN core.leagues l ON l.league_id = f.league_id
            JOIN core.teams ht ON ht.team_id = f.home_team_id
            JOIN core.teams at ON at.team_id = f.away_team_id
            WHERE fs.home_score IS NOT NULL AND f.kickoff_utc IS NOT NULL
            ORDER BY f.kickoff_utc ASC, f.fixture_id ASC
            """
        )
        rows = cursor.fetchall()

    states: dict[int, _TeamState] = {}
    n = 0
    hits = 0
    log_loss_sum = 0.0
    brier_sum = 0.0
    calib: dict[str, list[int]] = {}   # bucket -> [count, hits]
    by_family: dict[str, list[float]] = {}  # family -> [n, hits, logloss_sum]

    for kickoff, home_id, home_name, away_id, away_name, hs, aws, league in rows:
        kickoff = kickoff if kickoff.tzinfo else kickoff.replace(tzinfo=timezone.utc)
        home_id, away_id = int(home_id), int(away_id)
        hs, aws = int(hs), int(aws)
        home_state = states.setdefault(home_id, _TeamState())
        away_state = states.setdefault(away_id, _TeamState())

        in_window = evaluate_from <= kickoff <= evaluate_to
        enough_history = (
            len(home_state.history) >= MIN_PRIOR_MATCHES
            and len(away_state.history) >= MIN_PRIOR_MATCHES
        )

        if in_window and enough_history:
            neutral = is_neutral_venue(str(league))
            fixture = FixtureFeatures(
                fixture_id=0,
                home_team=_snapshot(home_id, str(home_name), home_state, kickoff),
                away_team=_snapshot(away_id, str(away_name), away_state, kickoff),
                home_advantage=0.04 if neutral else 0.16,
            )
            result = engine.analyze_fixture(fixture, None)
            probs = result.probabilities

            actual = "HOME" if hs > aws else "DRAW" if hs == aws else "AWAY"
            p_map = {"HOME": probs.home, "DRAW": probs.draw, "AWAY": probs.away}
            predicted = max(p_map, key=p_map.get)
            p_actual = max(1e-12, p_map[actual])

            if collect_records:
                records.append((kickoff.isoformat(), probs.home, probs.draw, probs.away, actual))

            if collect_derived:
                _, full_distribution = build_exact_score_distribution(
                    result.expected_home_goals,
                    result.expected_away_goals,
                    probs,
                    fixture=fixture,
                )
                derived = derived_market_probabilities(full_distribution)
                derived_records.append(
                    (
                        kickoff.isoformat(),
                        derived["OVER"],
                        derived["BTTS_YES"],
                        1 if hs + aws >= 3 else 0,
                        1 if hs > 0 and aws > 0 else 0,
                    )
                )

            n += 1
            hit = 1 if predicted == actual else 0
            hits += hit
            log_loss_sum += -math.log(p_actual)
            brier_sum += sum(
                (p_map[k] - (1.0 if k == actual else 0.0)) ** 2 for k in p_map
            )

            bucket = f"{int(p_map[predicted] * 10) * 10}-{int(p_map[predicted] * 10) * 10 + 10}%"
            c = calib.setdefault(bucket, [0, 0])
            c[0] += 1
            c[1] += hit

            family = competition_family(str(league))
            fam = by_family.setdefault(family, [0, 0, 0.0])
            fam[0] += 1
            fam[1] += hit
            fam[2] += -math.log(p_actual)

        # --- mise a jour de l'etat (TOUJOURS, meme hors fenetre) ---
        bonus = 0.0 if is_neutral_venue(str(league)) else HOME_BONUS_ELO
        expected_home = 1.0 / (1.0 + math.pow(10.0, -((home_state.rating - away_state.rating + bonus) / 400.0)))
        actual_score = 1.0 if hs > aws else 0.5 if hs == aws else 0.0
        k = BASE_K * competition_weight(str(league)) * _margin_multiplier(abs(hs - aws))
        delta = k * (actual_score - expected_home)
        home_state.rating += delta
        away_state.rating -= delta

        home_pts = 3.0 if hs > aws else 1.0 if hs == aws else 0.0
        away_pts = 3.0 if aws > hs else 1.0 if hs == aws else 0.0
        home_state.history.append((kickoff, str(league), float(hs), float(aws), home_pts))
        away_state.history.append((kickoff, str(league), float(aws), float(hs), away_pts))

    if n == 0:
        return {"error": "aucun match evaluable dans la fenetre", "evaluated": 0}

    calibration = {
        bucket: {
            "n": c[0],
            "realized_pct": round(100.0 * c[1] / c[0], 1),
        }
        for bucket, c in sorted(calib.items())
    }
    families = {
        fam: {
            "n": int(v[0]),
            "accuracy_pct": round(100.0 * v[1] / v[0], 1),
            "log_loss": round(v[2] / v[0], 4),
        }
        for fam, v in sorted(by_family.items())
    }
    report: dict[str, Any] = {
        "evaluated": n,
        "accuracy_pct": round(100.0 * hits / n, 2),
        "log_loss": round(log_loss_sum / n, 4),
        "brier": round(brier_sum / n, 4),
        "calibration": calibration,
        "by_family": families,
        "calibration_alpha": engine._calibration_alpha,
        "note": "moteur analytique seul (Poisson+Elo), sans marche ni XGBoost - zero fuite temporelle",
    }
    if collect_records:
        report["records"] = records
    if collect_derived:
        report["derived_records"] = derived_records
    return report
