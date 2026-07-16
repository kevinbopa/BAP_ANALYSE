"""Train the XGBoost 1X2 multi-class classifier from historical fixtures.

Pulls every completed fixture from ``core.fixtures`` joined with
``core.fixture_scores`` since a configurable cutoff year (default 2015),
computes features as of each fixture's kickoff (point-in-time, no leakage),
and saves the trained model to ``services/prediction/models/xgboost_1x2.joblib``.

Usage::

    py services/prediction/train_xgboost.py
    py services/prediction/train_xgboost.py --cutoff-year 2018 --min-prior-matches 5

The runtime engine picks the model up automatically on next pipeline run
when constructed with::

    from spe_prediction.gboost import XGBoostConfig, XGBoostPredictor
    predictor = XGBoostPredictor(XGBoostConfig(model_path="services/prediction/models/xgboost_1x2.joblib"))
    engine = MatchAnalysisEngine(xgboost_predictor=predictor)

KNOWN LIMITATION — point-in-time correctness
============================================
The current snapshot routine in ``spe_prediction.repository`` uses the
LAST 80 matches of a team regardless of dates. For training we want the
features **as of** the kickoff of the training fixture, otherwise the
target leaks into the features. This script therefore re-implements a
date-bounded variant of the layered snapshot, scoped to matches BEFORE
the training fixture's kickoff_utc.
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

# Make ``spe_prediction`` importable when the script is invoked directly.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "prediction" / "src"))
sys.path.insert(0, str(ROOT / "services" / "ingestion" / "src"))

from spe_prediction.db import DatabaseSettings, connect_db
from spe_prediction.gboost import DEFAULT_FEATURE_NAMES


WINNER_TO_LABEL = {"HOME": 0, "DRAW": 1, "AWAY": 2}
RECENCY_HALF_LIFE_DAYS = 365.0


@dataclass(frozen=True)
class TrainingArgs:
    cutoff_year: int
    min_prior_matches: int
    model_output: Path
    test_size: float
    league_filter: str | None


def parse_args(argv: Sequence[str] | None = None) -> TrainingArgs:
    parser = argparse.ArgumentParser(description="Train XGBoost 1X2 classifier")
    parser.add_argument("--cutoff-year", type=int, default=2015,
                        help="Earliest kickoff year to include in training (default 2015)")
    parser.add_argument("--min-prior-matches", type=int, default=8,
                        help="Skip training fixtures whose teams have fewer prior matches than this")
    parser.add_argument("--model-output", type=Path,
                        default=Path("services/prediction/models/xgboost_1x2.joblib"),
                        help="Where to save the trained joblib model")
    parser.add_argument("--test-size", type=float, default=0.20,
                        help="Fraction of samples reserved for the holdout set")
    parser.add_argument("--league-filter", type=str, default=None,
                        help="Optional substring match on league_name (case-insensitive)")
    ns = parser.parse_args(argv)
    return TrainingArgs(
        cutoff_year=ns.cutoff_year,
        min_prior_matches=ns.min_prior_matches,
        model_output=ns.model_output,
        test_size=ns.test_size,
        league_filter=ns.league_filter,
    )


def _recency_weight(kickoff: datetime, ref: datetime) -> float:
    age_days = max(0.0, (ref - kickoff).total_seconds() / 86400.0)
    return float(math.pow(0.5, age_days / RECENCY_HALF_LIFE_DAYS))


def _competition_weight(league_name: str) -> float:
    lowered = (league_name or "").lower()
    if any(kw in lowered for kw in ("u-23", "u23", "u-21", "u21", "u-20", "u20")):
        return 0.65
    if "friendly" in lowered:
        return 0.78
    if "world cup" in lowered and "qualif" not in lowered:
        return 1.30
    if any(kw in lowered for kw in ("euro", "copa america", "africa cup", "afcon", "asian cup", "gold cup", "confederations cup", "arab cup")):
        return 1.22
    if "nations league" in lowered:
        return 1.12
    if any(kw in lowered for kw in ("qualification", "qualifier")):
        return 1.10
    if "olympic" in lowered:
        return 0.85
    return 1.0


def _build_team_snapshot(cursor, team_id: int, as_of: datetime) -> dict[str, float]:
    """Snapshot of team strength as of ``as_of`` (exclusive).

    Returns Elo, attack/defense ratings, recent form and data_quality
    computed only from matches **strictly before** ``as_of``.
    """
    cursor.execute(
        """
        WITH team_matches AS (
            SELECT
                f.kickoff_utc,
                l.league_name,
                CASE
                    WHEN f.home_team_id = %(team_id)s THEN fs.home_score
                    ELSE fs.away_score
                END AS goals_for,
                CASE
                    WHEN f.home_team_id = %(team_id)s THEN fs.away_score
                    ELSE fs.home_score
                END AS goals_against,
                CASE
                    WHEN fs.home_score IS NULL OR fs.away_score IS NULL THEN NULL
                    WHEN (CASE WHEN f.home_team_id = %(team_id)s THEN fs.home_score ELSE fs.away_score END)
                       > (CASE WHEN f.home_team_id = %(team_id)s THEN fs.away_score ELSE fs.home_score END) THEN 1.0
                    WHEN (CASE WHEN f.home_team_id = %(team_id)s THEN fs.home_score ELSE fs.away_score END)
                       = (CASE WHEN f.home_team_id = %(team_id)s THEN fs.away_score ELSE fs.home_score END) THEN 0.5
                    ELSE 0.0
                END AS result_points
            FROM core.fixtures f
            JOIN core.leagues l ON l.league_id = f.league_id
            JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
            WHERE (f.home_team_id = %(team_id)s OR f.away_team_id = %(team_id)s)
              AND fs.home_score IS NOT NULL AND fs.away_score IS NOT NULL
              AND f.kickoff_utc IS NOT NULL
              AND f.kickoff_utc < %(as_of)s
            ORDER BY f.kickoff_utc DESC
            LIMIT 80
        )
        SELECT kickoff_utc, league_name, goals_for, goals_against, result_points
        FROM team_matches
        """,
        {"team_id": team_id, "as_of": as_of},
    )
    rows = cursor.fetchall()
    if not rows:
        return {
            "elo_rating": 1500.0,
            "attack_rating": 0.20,
            "defense_rating": 1.20,
            "recent_form": 0.0,
            "data_quality": 0.0,
            "played_matches": 0,
        }

    total_w = 0.0
    sum_gf = 0.0
    sum_ga = 0.0
    sum_pts = 0.0
    for kickoff, league, gf, ga, pts in rows:
        if gf is None or ga is None or pts is None:
            continue
        recency = _recency_weight(kickoff, as_of)
        comp = _competition_weight(str(league or ""))
        w = recency * comp
        if w <= 0:
            continue
        total_w += w
        sum_gf += float(gf) * w
        sum_ga += float(ga) * w
        sum_pts += float(pts) * w

    if total_w <= 0:
        return {
            "elo_rating": 1500.0,
            "attack_rating": 0.20,
            "defense_rating": 1.20,
            "recent_form": 0.0,
            "data_quality": 0.0,
            "played_matches": 0,
        }

    avg_gf = sum_gf / total_w
    avg_ga = sum_ga / total_w
    avg_pts = sum_pts / total_w
    elo = (
        1500.0
        + (avg_pts - 0.5) * 400.0
        + (avg_gf - avg_ga) * 35.0
    )
    data_quality = max(0.0, min(1.0, total_w / 12.0))
    return {
        "elo_rating": elo,
        "attack_rating": max(0.05, avg_gf - 1.0),
        "defense_rating": max(0.05, avg_ga),
        "recent_form": (avg_pts - 0.5) * 2.0,
        "data_quality": data_quality,
        "played_matches": len(rows),
    }


def _home_advantage_for_league(league_name: str) -> float:
    return 0.04 if "world cup" in (league_name or "").lower() else 0.16


def build_training_set(connection, args: TrainingArgs) -> tuple[list[list[float]], list[int]]:
    cutoff_dt = datetime(args.cutoff_year, 1, 1, tzinfo=timezone.utc)
    X: list[list[float]] = []
    y: list[int] = []

    with connection.cursor() as cursor:
        league_clause = ""
        params: list[object] = [cutoff_dt]
        if args.league_filter:
            league_clause = "AND lower(l.league_name) LIKE %s"
            params.append(f"%{args.league_filter.lower()}%")

        cursor.execute(
            f"""
            SELECT
                f.fixture_id,
                f.kickoff_utc,
                f.home_team_id,
                f.away_team_id,
                l.league_name,
                fs.winner_code
            FROM core.fixtures f
            JOIN core.leagues l ON l.league_id = f.league_id
            JOIN core.fixture_scores fs ON fs.fixture_id = f.fixture_id
            WHERE fs.winner_code IN ('HOME', 'DRAW', 'AWAY')
              AND f.kickoff_utc >= %s
              {league_clause}
            ORDER BY f.kickoff_utc ASC
            """,
            params,
        )
        rows = cursor.fetchall()

        for fixture_id, kickoff, home_id, away_id, league, winner in rows:
            if kickoff is None:
                continue
            home = _build_team_snapshot(cursor, int(home_id), kickoff)
            away = _build_team_snapshot(cursor, int(away_id), kickoff)
            if home["played_matches"] < args.min_prior_matches or away["played_matches"] < args.min_prior_matches:
                continue
            home_adv = _home_advantage_for_league(str(league or ""))
            features = [
                home["elo_rating"],
                away["elo_rating"],
                home["elo_rating"] - away["elo_rating"],
                home["attack_rating"],
                away["attack_rating"],
                home["defense_rating"],
                away["defense_rating"],
                home["recent_form"],
                away["recent_form"],
                0.0,  # fatigue_home (unknown at training time)
                0.0,  # fatigue_away
                home_adv,
                home["data_quality"],
                away["data_quality"],
                1 / 3, 1 / 3, 1 / 3,  # implied — unknown for historical training (no archived odds)
                0.10,
                0.0,
            ]
            X.append(features)
            y.append(WINNER_TO_LABEL[winner])

    return X, y


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import numpy as np
        import xgboost as xgb
        from sklearn.metrics import accuracy_score, log_loss
        from sklearn.model_selection import train_test_split
        import joblib
    except ImportError as exc:
        print(f"Missing dependency: {exc}. Install with:")
        print("  pip install -r services/prediction/requirements.txt")
        return 2

    print("Connecting to database...")
    connection = connect_db(DatabaseSettings.from_env())
    try:
        print(f"Building training set (cutoff_year={args.cutoff_year}, min_prior_matches={args.min_prior_matches})...")
        X, y = build_training_set(connection, args)
    finally:
        connection.close()

    if len(X) < 50:
        print(f"Only {len(X)} samples available — need at least 50 to train meaningfully.")
        print("Run `Cycle complet V1` from the dashboard to ingest more history first.")
        return 1

    X_arr = np.array(X, dtype=float)
    y_arr = np.array(y, dtype=int)
    print(f"Loaded {len(X_arr)} samples · class balance: HOME={sum(y_arr==0)} DRAW={sum(y_arr==1)} AWAY={sum(y_arr==2)}")

    X_tr, X_te, y_tr, y_te = train_test_split(
        X_arr, y_arr, test_size=args.test_size, random_state=42, stratify=y_arr,
    )

    model = xgb.XGBClassifier(
        objective="multi:softprob",
        num_class=3,
        n_estimators=300,
        max_depth=4,
        learning_rate=0.06,
        subsample=0.9,
        colsample_bytree=0.9,
        random_state=42,
        eval_metric="mlogloss",
    )
    print("Training XGBoost classifier...")
    model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], verbose=False)

    proba_te = model.predict_proba(X_te)
    pred_te = proba_te.argmax(axis=1)
    print(f"Holdout accuracy: {accuracy_score(y_te, pred_te):.3f}")
    print(f"Holdout log loss: {log_loss(y_te, proba_te):.3f}")

    args.model_output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, str(args.model_output))
    print(f"Model saved to {args.model_output}")
    print("Wire it into the engine via XGBoostPredictor(XGBoostConfig(model_path=...))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
