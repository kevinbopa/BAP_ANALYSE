from __future__ import annotations

PREDICTION_INSERT_SQL = """
INSERT INTO model.predictions (
    fixture_id,
    model_run_id,
    model_name,
    model_version,
    generated_at,
    home_win_probability,
    draw_probability,
    away_win_probability,
    expected_home_goals,
    expected_away_goals,
    confidence_score,
    feature_snapshot_json
)
VALUES (
    %(fixture_id)s,
    %(model_run_id)s,
    %(model_name)s,
    %(model_version)s,
    %(generated_at)s,
    %(home_win_probability)s,
    %(draw_probability)s,
    %(away_win_probability)s,
    %(expected_home_goals)s,
    %(expected_away_goals)s,
    %(confidence_score)s,
    %(feature_snapshot_json)s::jsonb
)
RETURNING prediction_id;
""".strip()

VALUE_BET_INSERT_SQL = """
INSERT INTO model.value_bets (
    fixture_id,
    prediction_id,
    bookmaker_id,
    selection_code,
    market_code,
    model_probability,
    implied_probability,
    edge_probability,
    fair_odd,
    market_odd,
    line,
    detected_at,
    status_code
)
VALUES (
    %(fixture_id)s,
    %(prediction_id)s,
    %(bookmaker_id)s,
    %(selection_code)s,
    %(market_code)s,
    %(model_probability)s,
    %(implied_probability)s,
    %(edge_probability)s,
    %(fair_odd)s,
    %(market_odd)s,
    %(line)s,
    %(detected_at)s,
    %(status_code)s
)
RETURNING value_bet_id;
""".strip()

DEAL_RANKING_INSERT_SQL = """
INSERT INTO model.deal_rankings (
    analysis_scope_id,
    fixture_id,
    prediction_id,
    value_bet_id,
    ranking_score,
    confidence_score,
    data_quality_score,
    freshness_score,
    rank_position,
    summary_reason,
    generated_at
)
VALUES (
    %(analysis_scope_id)s,
    %(fixture_id)s,
    %(prediction_id)s,
    %(value_bet_id)s,
    %(ranking_score)s,
    %(confidence_score)s,
    %(data_quality_score)s,
    %(freshness_score)s,
    %(rank_position)s,
    %(summary_reason)s,
    %(generated_at)s
);
""".strip()
