"""Calibre les probabilites derivees (over 2.5, BTTS) sur l'historique reel.

    py services/prediction/run_derived_calibration_fit.py

Pourquoi : la matrice Poisson/Dixon-Coles sous-disperse les buts (la variance
reelle depasse Poisson) — les P(over)/P(BTTS) sommees de la matrice portent
donc un biais SYSTEMATIQUE, mesurable sur des dizaines de milliers de matchs
avec le score final en base.

Methode (meme rigueur que la calibration 1X2) :
  - une passe walk-forward (zero fuite) sur le MOTEUR CALIBRE (la chaine que
    la prod utilise vraiment), qui collecte P_brut(over), P_brut(btts) et
    l'issue reelle de chaque match ;
  - split temporel strict FIT (2023-01 -> 2025-06) / HOLDOUT (2025-07 -> now) ;
  - grid search d'un alpha binaire par marche (p' = p^a / (p^a + (1-p)^a)),
    minimisant le log loss du FIT ;
  - l'alpha n'est sauvegarde (models/calibration_derived.json) QUE s'il bat
    l'identite sur le HOLDOUT jamais vu. Sinon rien ne change.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_prediction.backtest import run_backtest
from spe_prediction.db import DatabaseSettings, connect_db
from spe_prediction.derived_markets import DERIVED_CALIBRATION_PATH, binary_sharpen

FIT_FROM = datetime(2023, 1, 1, tzinfo=timezone.utc)
HOLDOUT_FROM = datetime(2025, 7, 1, tzinfo=timezone.utc)

# Grille elargie vers le bas : O/U et BTTS butaient sur l'ancienne borne 0.50,
# signe que l'alpha optimal est plus bas (marches derives peu discriminants ->
# plus de compression vers 0.5). Reste holdout-valide : rien n'est retenu si
# le log-loss ne s'ameliore pas.
ALPHA_GRID = [round(0.20 + 0.05 * i, 2) for i in range(37)]  # 0.20 -> 2.00


def _binary_log_loss(records: list[tuple[float, int]], alpha: float) -> float:
    total = 0.0
    for probability, outcome in records:
        p = binary_sharpen(probability, alpha)
        p = min(1.0 - 1e-12, max(1e-12, p))
        total += -math.log(p if outcome == 1 else 1.0 - p)
    return total / len(records)


def _fit_alpha(records: list[tuple[float, int]]) -> float:
    return min(ALPHA_GRID, key=lambda a: _binary_log_loss(records, a))


def _reliability_table(records: list[tuple[float, int]], alpha: float) -> dict[str, dict]:
    """Frequence realisee par tranche de probabilite predite (fiabilite)."""
    buckets: dict[str, list[int]] = {}
    for probability, outcome in records:
        p = binary_sharpen(probability, alpha)
        bucket = f"{int(p * 10) * 10}-{int(p * 10) * 10 + 10}%"
        c = buckets.setdefault(bucket, [0, 0])
        c[0] += 1
        c[1] += outcome
    return {
        b: {"n": c[0], "realized_pct": round(100.0 * c[1] / c[0], 1)}
        for b, c in sorted(buckets.items())
    }


def main() -> int:
    connection = connect_db(DatabaseSettings.from_env())
    try:
        # Moteur CALIBRE (alpha/draw_boost 1X2 charges) : on ajuste le residu
        # derive de la chaine reellement en production.
        report = run_backtest(
            connection, evaluate_from=FIT_FROM,
            calibration_alpha=None, collect_derived=True,
        )
    finally:
        connection.close()

    derived = report.pop("derived_records", [])
    result: dict = {"evaluated": report.get("evaluated", 0)}
    payload: dict = {}

    for market, prob_idx, outcome_idx in (("OU25", 1, 3), ("BTTS", 2, 4)):
        fit = [(r[prob_idx], r[outcome_idx]) for r in derived
               if datetime.fromisoformat(r[0]) < HOLDOUT_FROM]
        holdout = [(r[prob_idx], r[outcome_idx]) for r in derived
                   if datetime.fromisoformat(r[0]) >= HOLDOUT_FROM]
        if len(fit) < 500 or len(holdout) < 200:
            result[market] = {"error": "echantillons insuffisants",
                              "fit_n": len(fit), "holdout_n": len(holdout)}
            continue

        alpha = _fit_alpha(fit)
        holdout_identity = round(_binary_log_loss(holdout, 1.0), 4)
        holdout_fitted = round(_binary_log_loss(holdout, alpha), 4)
        keep = holdout_fitted < holdout_identity

        result[market] = {
            "alpha_ajuste": alpha,
            "fit_n": len(fit),
            "holdout_n": len(holdout),
            "holdout_log_loss_identite": holdout_identity,
            "holdout_log_loss_calibre": holdout_fitted,
            "retenu": keep,
            "fiabilite_holdout_avant": _reliability_table(holdout, 1.0),
            "fiabilite_holdout_apres": _reliability_table(holdout, alpha if keep else 1.0),
        }
        if keep:
            payload[market] = {"alpha": alpha}

    if payload:
        metadata = {
            "fitted_at": datetime.now(timezone.utc).isoformat(),
            "method": "binary sharpen p^a/(p^a+(1-p)^a), grid 0.50-2.00, "
                      "fit 2023-01->2025-06, arbitrage holdout 2025-07->now",
            "engine": "walk-forward calibre (alpha/draw_boost 1X2 charges)",
        }
        DERIVED_CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        DERIVED_CALIBRATION_PATH.write_text(
            json.dumps({**payload, "metadata": metadata}, indent=2), encoding="utf-8"
        )
        result["sauvegarde"] = str(DERIVED_CALIBRATION_PATH)
    else:
        result["sauvegarde"] = "NON - aucun alpha ne bat l'identite sur le holdout"

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
