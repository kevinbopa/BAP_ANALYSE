"""Cadre generique : UN modele dedie par marche de paris.

Chaque marche = (cle, fonction-label, libelle). Tous les marches "buts"
partagent le meme snapshot d'equipe point-in-time et le meme vecteur de
features ; seule l'ISSUE (label) change, donc un seul passage sur les donnees
entraine tous les modeles (train_market_model.py).

Runtime : predict_market(cle, ...) -> proba, ou predict_derived_probabilities
(compat O/U 2.5 + BTTS pour le path deals). Degrade a None si modele/donnees
manquent -> l'appelant retombe sur le Poisson-derive.

Ajouter un marche exploitable = une ligne dans MARKET_REGISTRY (+ des cotes).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_MODEL_DIR = Path(__file__).resolve().parents[2] / "models"
_HALF_LIFE_DAYS = 365.0
_MIN_PRIOR = 8
_cache: dict[str, object] = {}


@dataclass(frozen=True)
class MarketSpec:
    key: str
    # (home_score, away_score) -> 0/1
    label_fn: Callable[[int, int], int]
    display: str


# --- Registre des marches buts exploitables (label = score final) -----------
MARKET_REGISTRY: dict[str, MarketSpec] = {
    "over15": MarketSpec("over15", lambda h, a: 1 if h + a >= 2 else 0, "Plus de 1.5 buts"),
    "over25": MarketSpec("over25", lambda h, a: 1 if h + a >= 3 else 0, "Plus de 2.5 buts"),
    "over35": MarketSpec("over35", lambda h, a: 1 if h + a >= 4 else 0, "Plus de 3.5 buts"),
    "btts": MarketSpec("btts", lambda h, a: 1 if h >= 1 and a >= 1 else 0, "Les deux marquent"),
    "home_over15": MarketSpec("home_over15", lambda h, a: 1 if h >= 2 else 0, "Domicile +1.5 but"),
    "away_over15": MarketSpec("away_over15", lambda h, a: 1 if a >= 2 else 0, "Exterieur +1.5 but"),
}

# NOTE xG (2026-07-11, mesure) : ajouter le xG equipe (xg_for/against recents,
# 6 features + drapeaux) n'apporte RIEN — meme sur le sous-ensemble couvert
# Big-5 (2300 matchs holdout : deltas -0.0017..+0.0003 = bruit). Les taux de
# buts ponderes par recence sur 60 matchs capturent deja le signal. Le +0.027
# du premier A/B etait un artefact de petit echantillon (518 matchs de train).
# On reste a 20 features ; l'ingestion team_match_stats continue (futurs
# marches corners/cartons).
FEATURE_NAMES = [
    "home_attack", "away_attack", "home_defense", "away_defense",
    "home_over15_rate", "away_over15_rate", "home_over25_rate", "away_over25_rate",
    "home_over35_rate", "away_over35_rate", "home_btts_rate", "away_btts_rate",
    "home_team_score15_rate", "away_team_score15_rate",
    "home_avg_total", "away_avg_total", "expected_total", "home_adv",
    "home_data_quality", "away_data_quality",
]


def _recency_weight(kickoff: datetime, as_of: datetime) -> float:
    if kickoff is None:
        return 0.0
    days = (as_of - kickoff).total_seconds() / 86400.0
    if days < 0:
        return 0.0
    return 0.5 ** (days / _HALF_LIFE_DAYS)


def _competition_weight(league_name: str) -> float:
    lowered = (league_name or "").lower()
    if "world cup" in lowered and "qualif" not in lowered:
        return 1.30
    if any(k in lowered for k in ("euro", "copa america", "africa cup", "afcon",
                                  "asian cup", "gold cup", "confederations cup", "arab cup")):
        return 1.22
    if "nations league" in lowered:
        return 1.12
    if any(k in lowered for k in ("qualification", "qualifier")):
        return 1.10
    if "olympic" in lowered:
        return 0.85
    return 1.0


def team_goals_snapshot(cursor, team_id: int, as_of: datetime) -> dict[str, float] | None:
    """Profil BUTS point-in-time (matchs strictement avant as_of).

    Attaque/defense + taux over 1.5/2.5/3.5, BTTS, et taux ou l'equipe marque
    2+ (pour les totals d'equipe). None si trop peu de matchs fiables."""
    cursor.execute(
        """
        SELECT f.kickoff_utc, l.league_name,
            CASE WHEN f.home_team_id = %(t)s THEN fs.home_score ELSE fs.away_score END AS gf,
            CASE WHEN f.home_team_id = %(t)s THEN fs.away_score ELSE fs.home_score END AS ga
        FROM core.fixtures f
        JOIN core.leagues l ON l.league_id = f.league_id
        JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
        WHERE (f.home_team_id = %(t)s OR f.away_team_id = %(t)s)
          AND fs.home_score IS NOT NULL AND fs.away_score IS NOT NULL
          AND f.kickoff_utc IS NOT NULL AND f.kickoff_utc < %(as_of)s
        ORDER BY f.kickoff_utc DESC
        LIMIT 60
        """,
        {"t": team_id, "as_of": as_of},
    )
    tw = gf_s = ga_s = tot_s = o15 = o25 = o35 = btts = score15 = 0.0
    n = 0
    for kickoff, league, gf, ga in cursor.fetchall():
        if gf is None or ga is None:
            continue
        w = _recency_weight(kickoff, as_of) * _competition_weight(str(league or ""))
        if w <= 0:
            continue
        gf, ga = float(gf), float(ga)
        total = gf + ga
        tw += w
        gf_s += gf * w
        ga_s += ga * w
        tot_s += total * w
        o15 += (1.0 if total >= 2 else 0.0) * w
        o25 += (1.0 if total >= 3 else 0.0) * w
        o35 += (1.0 if total >= 4 else 0.0) * w
        btts += (1.0 if gf >= 1 and ga >= 1 else 0.0) * w
        score15 += (1.0 if gf >= 2 else 0.0) * w
        n += 1
    if n < _MIN_PRIOR or tw <= 0:
        return None
    return {
        "attack": gf_s / tw, "defense": ga_s / tw, "avg_total": tot_s / tw,
        "over15_rate": o15 / tw, "over25_rate": o25 / tw, "over35_rate": o35 / tw,
        "btts_rate": btts / tw, "team_score15_rate": score15 / tw,
        "data_quality": max(0.0, min(1.0, tw / 12.0)),
    }


def build_match_features(h: dict, a: dict, home_adv: float) -> list[float]:
    expected_total = (h["attack"] + a["defense"]) / 2 + (a["attack"] + h["defense"]) / 2
    return [
        h["attack"], a["attack"], h["defense"], a["defense"],
        h["over15_rate"], a["over15_rate"], h["over25_rate"], a["over25_rate"],
        h["over35_rate"], a["over35_rate"], h["btts_rate"], a["btts_rate"],
        h["team_score15_rate"], a["team_score15_rate"],
        h["avg_total"], a["avg_total"], expected_total, home_adv,
        h["data_quality"], a["data_quality"],
    ]


# --- Runtime ----------------------------------------------------------------
def _load_model(key: str):
    cache_key = f"model:{key}"
    if cache_key in _cache:
        return _cache[cache_key]
    model = None
    try:
        import joblib
        payload = joblib.load(str(_MODEL_DIR / f"xgboost_{key}.joblib"))
        model = payload["model"]
    except Exception:
        model = None
    _cache[cache_key] = model
    return model


def predict_market(
    key: str, cursor, home_team_id: int, away_team_id: int,
    as_of: datetime | None, home_adv: float,
) -> float | None:
    """Proba du marche ``key`` pour ce match. None si indisponible."""
    model = _load_model(key)
    if model is None:
        return None
    as_of = as_of or datetime.now(timezone.utc)
    h = team_goals_snapshot(cursor, int(home_team_id), as_of)
    a = team_goals_snapshot(cursor, int(away_team_id), as_of)
    if h is None or a is None:
        return None
    try:
        import numpy as np
        arr = np.array([build_match_features(h, a, home_adv)], dtype=float)
        p = float(model.predict_proba(arr)[0, 1])
    except Exception:
        return None
    return min(0.99, max(0.01, p))


def predict_derived_probabilities(
    cursor, home_team_id: int, away_team_id: int,
    as_of: datetime | None, home_adv: float,
) -> dict[str, float] | None:
    """Compat path deals O/U 2.5 + BTTS. Un seul snapshot pour les deux."""
    model_over = _load_model("over25")
    model_btts = _load_model("btts")
    if model_over is None or model_btts is None:
        return None
    as_of = as_of or datetime.now(timezone.utc)
    h = team_goals_snapshot(cursor, int(home_team_id), as_of)
    a = team_goals_snapshot(cursor, int(away_team_id), as_of)
    if h is None or a is None:
        return None
    try:
        import numpy as np
        arr = np.array([build_match_features(h, a, home_adv)], dtype=float)
        p_over = min(0.99, max(0.01, float(model_over.predict_proba(arr)[0, 1])))
        p_btts = min(0.99, max(0.01, float(model_btts.predict_proba(arr)[0, 1])))
    except Exception:
        return None
    return {"OVER": p_over, "UNDER": 1.0 - p_over,
            "BTTS_YES": p_btts, "BTTS_NO": 1.0 - p_btts}
