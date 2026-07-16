from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from spe_prediction.derived_markets import calibrated_derived_probabilities
from spe_prediction.domain import AnalysisResult, ExactScoreAnalysis, SelectionDecision


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def prediction_record(
    result: AnalysisResult,
    model_run_id: str | None = None,
    exact_score_analysis: ExactScoreAnalysis | None = None,
) -> dict[str, Any]:
    snapshot = {
        "poisson": result.poisson_probabilities.as_dict(),
        "elo": result.elo_probabilities.as_dict(),
        "explication": result.explanation,
        "pronostic": result.verdict.selection_code,
        "surete": round(result.verdict.surety_score, 4),
        "nul_competitif": result.verdict.draw_competitive,
    }
    if exact_score_analysis is not None:
        snapshot["exact_scores"] = [
            {
                "score": row.score_label,
                "probability_pct": round(row.probability * 100, 1),
                "fair_odd": round(row.fair_odd, 2),
                "outcome_code": row.outcome_code,
            }
            for row in exact_score_analysis.top_scores
        ]
        snapshot["exact_score_note"] = (
            "Score exact derive des xG, du draw bias, des absences, "
            "du contexte et des correlations validees."
        )
        derived = calibrated_derived_probabilities(exact_score_analysis.full_distribution)
        snapshot["marches_derives"] = {
            "over25_pct": round(derived["OVER"] * 100, 1),
            "under25_pct": round(derived["UNDER"] * 100, 1),
            "btts_oui_pct": round(derived["BTTS_YES"] * 100, 1),
            "btts_non_pct": round(derived["BTTS_NO"] * 100, 1),
        }

    return {
        "fixture_id": result.fixture_id,
        "model_run_id": model_run_id,
        "model_name": "poisson_market_hybrid_v1",
        "model_version": "1.2.0",
        "generated_at": _utc_now(),
        "home_win_probability": result.probabilities.home,
        "draw_probability": result.probabilities.draw,
        "away_win_probability": result.probabilities.away,
        "expected_home_goals": result.expected_home_goals,
        "expected_away_goals": result.expected_away_goals,
        "confidence_score": result.top_selection.confidence_score,
        "feature_snapshot_json": json.dumps(snapshot, ensure_ascii=True),
    }


def value_bet_record(
    result: AnalysisResult,
    selection: SelectionDecision,
    prediction_id: int,
    bookmaker_id: int | None = None,
) -> dict[str, Any]:
    return {
        "fixture_id": result.fixture_id,
        "prediction_id": prediction_id,
        "bookmaker_id": bookmaker_id if bookmaker_id is not None else selection.bookmaker_id,
        "selection_code": selection.selection_code,
        "market_code": selection.market_code,
        "model_probability": selection.model_probability,
        "implied_probability": selection.implied_probability,
        "edge_probability": selection.edge_probability,
        "fair_odd": selection.fair_odd,
        "market_odd": selection.market_odd,
        "line": selection.line,
        "detected_at": _utc_now(),
        "status_code": "ACTIVE",
    }


def ranking_record(
    result: AnalysisResult,
    selection: SelectionDecision,
    prediction_id: int,
    rank_position: int,
    analysis_scope_id: str,
    value_bet_id: int | None = None,
    data_quality_score: float = 0.75,
    freshness_score: float = 0.80,
) -> dict[str, Any]:
    return {
        "analysis_scope_id": analysis_scope_id,
        "fixture_id": result.fixture_id,
        "prediction_id": prediction_id,
        "value_bet_id": value_bet_id,
        "ranking_score": selection.ranking_score,
        "confidence_score": selection.confidence_score,
        "data_quality_score": data_quality_score,
        "freshness_score": freshness_score,
        "rank_position": rank_position,
        "summary_reason": selection.rationale,
        "generated_at": _utc_now(),
    }
