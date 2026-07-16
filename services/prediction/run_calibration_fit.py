"""Ajuste la calibration (alpha) sur le passe et la VALIDE sur un holdout.

    py services/prediction/run_calibration_fit.py

Split temporel strict :
  - FIT     : matchs 2023-01-01 -> 2025-06-30 (recherche de l'alpha optimal)
  - HOLDOUT : matchs 2025-07-01 -> maintenant (jamais vus par l'ajustement)

L'alpha n'est sauvegarde (models/calibration.json) QUE si le holdout confirme
l'amelioration du log loss. Sinon, rien ne change (alpha reste 1.0).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_prediction.backtest import run_backtest
from spe_prediction.calibration import (
    fit_alpha,
    fit_calibration,
    log_loss_for_alpha,
    save_calibration,
    sharpen,
)
from spe_prediction.db import DatabaseSettings, connect_db

FIT_FROM = datetime(2023, 1, 1, tzinfo=timezone.utc)
HOLDOUT_FROM = datetime(2025, 7, 1, tzinfo=timezone.utc)


def _bucket_table(records, alpha: float, draw_boost: float = 1.0) -> dict[str, dict]:
    buckets: dict[str, list[int]] = {}
    for ph, pd, pa, actual in records:
        h, d, a = sharpen(ph, pd, pa, alpha, draw_boost)
        p_map = {"HOME": h, "DRAW": d, "AWAY": a}
        predicted = max(p_map, key=p_map.get)
        top = p_map[predicted]
        bucket = f"{int(top * 10) * 10}-{int(top * 10) * 10 + 10}%"
        c = buckets.setdefault(bucket, [0, 0])
        c[0] += 1
        c[1] += 1 if predicted == actual else 0
    return {
        b: {"n": c[0], "realized_pct": round(100.0 * c[1] / c[0], 1)}
        for b, c in sorted(buckets.items())
    }


def main() -> int:
    connection = connect_db(DatabaseSettings.from_env())
    try:
        # Une seule passe walk-forward, probabilites BRUTES (alpha force a 1.0).
        report = run_backtest(
            connection, evaluate_from=FIT_FROM,
            calibration_alpha=1.0, collect_records=True,
        )
    finally:
        connection.close()

    records = report.pop("records")
    fit_records = [(r[1], r[2], r[3], r[4]) for r in records
                   if datetime.fromisoformat(r[0]) < HOLDOUT_FROM]
    holdout_records = [(r[1], r[2], r[3], r[4]) for r in records
                       if datetime.fromisoformat(r[0]) >= HOLDOUT_FROM]

    if len(fit_records) < 500 or len(holdout_records) < 200:
        print(json.dumps({"error": "echantillons insuffisants",
                          "fit_n": len(fit_records), "holdout_n": len(holdout_records)}))
        return 1

    # Trois candidats, departages HONNETEMENT sur le holdout jamais vu :
    #   identite (1,1) / alpha seul / alpha + draw_boost.
    alpha_only = fit_alpha(fit_records)
    alpha2, beta2 = fit_calibration(fit_records)

    candidates = {
        "identite": (1.0, 1.0),
        "alpha_seul": (alpha_only, 1.0),
        "alpha_plus_nul": (alpha2, beta2),
    }
    holdout_scores = {
        name: round(log_loss_for_alpha(holdout_records, a, b), 4)
        for name, (a, b) in candidates.items()
    }
    winner = min(holdout_scores, key=holdout_scores.get)
    best_alpha, best_beta = candidates[winner]

    result = {
        "candidats": {n: {"alpha": a, "draw_boost": b} for n, (a, b) in candidates.items()},
        "holdout_log_loss": holdout_scores,
        "gagnant": winner,
        "fit_n": len(fit_records),
        "holdout_n": len(holdout_records),
        "calibration_holdout_avant": _bucket_table(holdout_records, 1.0),
        "calibration_holdout_apres": _bucket_table(holdout_records, best_alpha, best_beta),
    }

    if winner != "identite":
        path = save_calibration(best_alpha, {
            "fitted_at": datetime.now(timezone.utc).isoformat(),
            "fit_n": len(fit_records),
            "holdout_n": len(holdout_records),
            "holdout_log_loss": holdout_scores,
            "winner": winner,
        }, draw_boost=best_beta)
        result["sauvegarde"] = str(path)
    else:
        result["sauvegarde"] = "NON - aucun candidat ne bat l'identite sur le holdout"

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
