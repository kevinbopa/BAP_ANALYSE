from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_prediction.db import DatabaseSettings, connect_db
from spe_prediction.engine import MatchAnalysisEngine
from spe_prediction.gboost import XGBoostConfig, XGBoostPredictor
from spe_prediction.pipeline import PredictionPipeline
from spe_prediction.repository import PostgresPredictionRepository


DEFAULT_XGBOOST_MODEL = Path(__file__).resolve().parent / "models" / "xgboost_1x2.joblib"


def _build_engine() -> MatchAnalysisEngine:
    """Build the engine with an XGBoost predictor wired in *if* a trained model
    file exists. The fallback path (no model file) produces the analytical
    Poisson + Elo + market blend used pre-XGBoost."""
    if DEFAULT_XGBOOST_MODEL.exists():
        predictor = XGBoostPredictor(XGBoostConfig(model_path=str(DEFAULT_XGBOOST_MODEL)))
        if predictor.available:
            return MatchAnalysisEngine(xgboost_predictor=predictor)
    return MatchAnalysisEngine()


def main() -> int:
    # py run_prediction_pipeline.py ["Nom de la competition"]
    # Sans argument : balayage global (7 jours, toutes competitions).
    league_name = sys.argv[1].strip() if len(sys.argv) > 1 else None
    connection = connect_db(DatabaseSettings.from_env())
    try:
        repository = PostgresPredictionRepository(connection, league_name=league_name)
        engine = _build_engine()
        processed = PredictionPipeline(repository, engine).run()
        repository.finalize_run()
        print(json.dumps({
            "processed_fixtures": processed,
            "league": league_name or "toutes",
            "xgboost_active": engine._xgboost is not None and engine._xgboost.available,
        }, indent=2))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
