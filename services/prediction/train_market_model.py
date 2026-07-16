"""Entraine UN modele dedie par marche buts, via le registre market_models.

Une seule passe sur les donnees : pour chaque match on calcule le snapshot des
deux equipes + le vecteur de features UNE fois, puis on en derive le label de
chaque marche enregistre. Split temporel (2025-07-01+) pour un holdout honnete.

    py services/prediction/train_market_model.py            # tous les marches
    py services/prediction/train_market_model.py over15 over35
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "prediction" / "src"))

from spe_prediction.db import DatabaseSettings, connect_db
from spe_prediction.market_models import (
    MARKET_REGISTRY,
    build_match_features,
    team_goals_snapshot,
)

HOLDOUT_FROM = datetime(2025, 7, 1, tzinfo=timezone.utc)
CUTOFF = datetime(2018, 1, 1, tzinfo=timezone.utc)
MODEL_DIR = ROOT / "services" / "prediction" / "models"


def _home_adv(league_name: str) -> float:
    return 0.04 if "world cup" in (league_name or "").lower() else 0.16


def build_dataset(connection):
    X: list[list[float]] = []
    scores: list[tuple[int, int]] = []
    dates: list[datetime] = []
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT f.kickoff_utc, f.home_team_id, f.away_team_id, l.league_name,
                   fs.home_score, fs.away_score
            FROM core.fixtures f
            JOIN core.leagues l ON l.league_id = f.league_id
            JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
            WHERE fs.home_score IS NOT NULL AND fs.away_score IS NOT NULL
              AND f.kickoff_utc >= %s
            ORDER BY f.kickoff_utc ASC
            """,
            (CUTOFF,),
        )
        for kickoff, home_id, away_id, league, hs, aws in cursor.fetchall():
            if kickoff is None:
                continue
            h = team_goals_snapshot(cursor, int(home_id), kickoff)
            a = team_goals_snapshot(cursor, int(away_id), kickoff)
            if h is None or a is None:
                continue
            X.append(build_match_features(h, a, _home_adv(str(league or ""))))
            scores.append((int(hs), int(aws)))
            dates.append(kickoff)
    return X, scores, dates


def _train(key, label_fn, X, scores, dates):
    import numpy as np
    import xgboost as xgb
    from sklearn.metrics import log_loss
    import joblib

    Xa = np.array(X, dtype=float)
    y = np.array([label_fn(h, a) for h, a in scores], dtype=int)
    hold = np.array([d >= HOLDOUT_FROM for d in dates])
    if y[hold].sum() in (0, len(y[hold])) or len(y[~hold]) < 500:
        return {"marche": key, "erreur": "classe degeneree ou trop peu de donnees"}

    model = xgb.XGBClassifier(
        objective="binary:logistic", n_estimators=350, max_depth=4,
        learning_rate=0.05, subsample=0.9, colsample_bytree=0.9,
        min_child_weight=5, random_state=42, eval_metric="logloss",
    )
    model.fit(Xa[~hold], y[~hold], eval_set=[(Xa[hold], y[hold])], verbose=False)
    p = model.predict_proba(Xa[hold])[:, 1]
    ll = log_loss(y[hold], p, labels=[0, 1])
    base = float(y[~hold].mean())
    base_ll = log_loss(y[hold], [base] * hold.sum(), labels=[0, 1])
    acc = float(((p >= 0.5).astype(int) == y[hold]).mean())

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model}, str(MODEL_DIR / f"xgboost_{key}.joblib"))
    return {
        "marche": key, "base_rate": round(base, 3), "accuracy": round(acc, 3),
        "log_loss_base": round(base_ll, 4), "log_loss_modele": round(ll, 4),
        "gain": round(base_ll - ll, 4), "bat_base": ll < base_ll,
    }


def main(argv=None) -> int:
    try:
        import xgboost  # noqa: F401
    except ImportError:
        print("xgboost manquant"); return 2
    keys = [a for a in (argv or sys.argv[1:]) if not a.startswith("-")] or list(MARKET_REGISTRY)
    connection = connect_db(DatabaseSettings.from_env())
    try:
        print("Construction du dataset (une passe)...")
        X, scores, dates = build_dataset(connection)
    finally:
        connection.close()
    results = {"samples": len(X), "marches": {}}
    for key in keys:
        spec = MARKET_REGISTRY.get(key)
        if spec is None:
            results["marches"][key] = {"erreur": "marche inconnu"}
            continue
        results["marches"][key] = _train(key, spec.label_fn, X, scores, dates)
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
