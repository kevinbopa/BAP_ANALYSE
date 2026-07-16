"""Couche de calibration des probabilites (temperature sharpening).

Le backtest walk-forward a revele une sous-confiance systematique du moteur
analytique : quand il annonce 60-70%, il gagne ~81% du temps. La correction :

    p_i' = p_i^alpha / sum_j p_j^alpha        (alpha > 1 = plus tranchant)

Proprietes : un seul parametre, preserve l'ordre des issues, alpha=1 = identite.

Discipline d'ajustement :
  - alpha est ajuste sur une fenetre PASSEE (fit) et valide sur un holdout
    posterieur jamais vu (run_calibration_fit.py) — pas d'auto-contemplation.
  - En production, la calibration s'applique a la composante MODELE seulement
    (Poisson+Elo+XGBoost), AVANT le melange avec l'ancre marche : le marche
    est deja calibre, le sur-affuter le fausserait.

Le parametre vit dans services/prediction/models/calibration.json ; absent =
alpha 1.0 (aucun effet), meme pattern gracieux que le modele XGBoost.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Sequence

DEFAULT_CALIBRATION_PATH = Path(__file__).resolve().parents[2] / "models" / "calibration.json"

# Record de backtest : (p_home, p_draw, p_away, actual_code)
Record = tuple[float, float, float, str]


def sharpen(
    home: float, draw: float, away: float,
    alpha: float, draw_boost: float = 1.0,
) -> tuple[float, float, float]:
    """Calibration a deux parametres (renormalisee).

    - ``alpha``      : temperature — affute (>1) ou adoucit (<1) les convictions.
    - ``draw_boost`` : multiplicateur du NUL — corrige le niveau structurel de
      l'issue du milieu, que la temperature seule ecrase (mesure : le modele
      calibre en alpha seul sous-ponderait le nul de ~8 pts vs le marche).
    """
    if alpha == 1.0 and draw_boost == 1.0:
        return home, draw, away
    eps = 1e-12
    h = max(eps, home) ** alpha
    d = (max(eps, draw) ** alpha) * draw_boost
    a = max(eps, away) ** alpha
    total = h + d + a
    return h / total, d / total, a / total


def log_loss_for_alpha(
    records: Sequence[Record], alpha: float, draw_boost: float = 1.0,
) -> float:
    if not records:
        return float("inf")
    total = 0.0
    for ph, pd, pa, actual in records:
        h, d, a = sharpen(ph, pd, pa, alpha, draw_boost)
        p_actual = {"HOME": h, "DRAW": d, "AWAY": a}[actual]
        total += -math.log(max(1e-12, p_actual))
    return total / len(records)


def fit_alpha(
    records: Sequence[Record],
    alpha_min: float = 0.80,
    alpha_max: float = 2.20,
    step: float = 0.05,
) -> float:
    """Recherche l'alpha seul minimisant le log loss (draw_boost fixe a 1)."""
    return fit_calibration(records, alpha_min, alpha_max, step, beta_min=1.0, beta_max=1.0)[0]


def fit_calibration(
    records: Sequence[Record],
    alpha_min: float = 0.80,
    alpha_max: float = 2.20,
    alpha_step: float = 0.05,
    beta_min: float = 0.70,
    beta_max: float = 1.60,
    beta_step: float = 0.05,
) -> tuple[float, float]:
    """Grille 2D (alpha, draw_boost) minimisant le log loss."""
    best = (1.0, 1.0)
    best_loss = float("inf")
    alpha = alpha_min
    while alpha <= alpha_max + 1e-9:
        beta = beta_min
        while beta <= beta_max + 1e-9:
            loss = log_loss_for_alpha(records, round(alpha, 4), round(beta, 4))
            if loss < best_loss:
                best_loss = loss
                best = (round(alpha, 4), round(beta, 4))
            beta += beta_step
        alpha += alpha_step
    return best


def load_calibration(path: Path | None = None) -> tuple[float, float]:
    """(alpha, draw_boost) ajustes ; (1.0, 1.0) = identite si aucun fichier.

    Garde-fous : des valeurs aberrantes (corruption, edition manuelle) sont
    ignorees et retombent sur l'identite.
    """
    target = path or DEFAULT_CALIBRATION_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        alpha = float(payload.get("alpha", 1.0))
        draw_boost = float(payload.get("draw_boost", 1.0))
        if not (0.5 <= alpha <= 3.0) or not (0.4 <= draw_boost <= 2.5):
            return 1.0, 1.0
        return alpha, draw_boost
    except (OSError, ValueError, json.JSONDecodeError):
        return 1.0, 1.0


def load_calibration_alpha(path: Path | None = None) -> float:
    """Compat : alpha seul."""
    return load_calibration(path)[0]


def save_calibration(
    alpha: float, metadata: dict, path: Path | None = None, draw_boost: float = 1.0,
) -> Path:
    target = path or DEFAULT_CALIBRATION_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"alpha": alpha, "draw_boost": draw_boost, **metadata},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return target
