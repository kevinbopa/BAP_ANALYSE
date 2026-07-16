"""Le match nul est-il un pronostic mis de cote ? Mesure et remede.

    py services/prediction/run_draw_verdict_analysis.py

1. Mesure sur l'historique walk-forward (moteur calibre, zero fuite) :
   part des verdicts DRAW avec la regle actuelle (argmax pur) vs frequence
   reelle des nuls, et fiabilite de p_draw par tranche.
2. Teste des regles alternatives "DRAW si p_draw >= max - epsilon et
   p_draw >= plancher" : impact accuracy global + volume et reussite des
   pronostics nuls. La regle n'est retenue que si elle rend le nul vivant
   SANS degrader l'accuracy au-dela du bruit.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_prediction.backtest import run_backtest
from spe_prediction.db import DatabaseSettings, connect_db

EVALUATE_FROM = datetime(2023, 1, 1, tzinfo=timezone.utc)


def main() -> int:
    connection = connect_db(DatabaseSettings.from_env())
    try:
        report = run_backtest(
            connection, evaluate_from=EVALUATE_FROM,
            calibration_alpha=None, collect_records=True,
        )
    finally:
        connection.close()

    records = report.pop("records")
    n = len(records)
    draws_real = sum(1 for _, _, _, _, actual in records if actual == "DRAW")

    # --- Etat actuel : argmax pur -------------------------------------------
    argmax_hits = 0
    argmax_draw_predicted = 0
    argmax_draw_hits = 0
    for _, ph, pd, pa, actual in records:
        predicted = max((("HOME", ph), ("DRAW", pd), ("AWAY", pa)), key=lambda kv: kv[1])[0]
        if predicted == actual:
            argmax_hits += 1
        if predicted == "DRAW":
            argmax_draw_predicted += 1
            if actual == "DRAW":
                argmax_draw_hits += 1

    # --- Fiabilite de p_draw par tranche ------------------------------------
    reliability: dict[str, list[int]] = {}
    for _, _, pd, _, actual in records:
        bucket = f"{int(pd * 20) * 5}-{int(pd * 20) * 5 + 5}%"
        c = reliability.setdefault(bucket, [0, 0])
        c[0] += 1
        c[1] += 1 if actual == "DRAW" else 0

    # --- Regles alternatives --------------------------------------------------
    rules = {}
    for epsilon in (0.01, 0.02, 0.03, 0.05):
        for floor in (0.28, 0.30, 0.32):
            hits = 0
            draw_predicted = 0
            draw_hits = 0
            for _, ph, pd, pa, actual in records:
                top = max(ph, pa)
                if pd >= top - epsilon and pd >= floor:
                    predicted = "DRAW"
                else:
                    predicted = "HOME" if ph >= pa else "AWAY"
                    predicted = max((("HOME", ph), ("DRAW", pd), ("AWAY", pa)),
                                    key=lambda kv: kv[1])[0]
                if predicted == actual:
                    hits += 1
                if predicted == "DRAW":
                    draw_predicted += 1
                    if actual == "DRAW":
                        draw_hits += 1
            rules[f"eps={epsilon:.2f},plancher={floor:.2f}"] = {
                "accuracy_pct": round(100.0 * hits / n, 2),
                "verdicts_nuls": draw_predicted,
                "verdicts_nuls_pct": round(100.0 * draw_predicted / n, 1),
                "reussite_nuls_pct": (
                    round(100.0 * draw_hits / draw_predicted, 1) if draw_predicted else None
                ),
            }

    result = {
        "matchs_evalues": n,
        "nuls_reels_pct": round(100.0 * draws_real / n, 1),
        "regle_actuelle_argmax": {
            "accuracy_pct": round(100.0 * argmax_hits / n, 2),
            "verdicts_nuls": argmax_draw_predicted,
            "verdicts_nuls_pct": round(100.0 * argmax_draw_predicted / n, 1),
            "reussite_nuls_pct": (
                round(100.0 * argmax_draw_hits / argmax_draw_predicted, 1)
                if argmax_draw_predicted else None
            ),
        },
        "fiabilite_p_draw": {
            b: {"n": c[0], "nuls_reels_pct": round(100.0 * c[1] / c[0], 1)}
            for b, c in sorted(reliability.items()) if c[0] >= 30
        },
        "regles_alternatives": rules,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
