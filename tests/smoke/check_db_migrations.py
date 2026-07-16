from __future__ import annotations

from pathlib import Path
import sys


EXPECTED_FILES = [
    "0001_v1_baseline.sql",
    "0002_v1_security.sql",
    "0003_v1_stake_provider.sql",
    "0004_v1_hardening.sql",
    "0005_v1_prediction_security.sql",
    "0006_v1_stake_odds_contract.sql",
    "0007_v1_theoddsapi_provider.sql",
    "0017_v1_annotation_kind_scope.sql",
    "0018_v1_taken_price_and_real_profit.sql",
    "0019_v1_position_tickets_and_pronostic_markets.sql",
    "0020_v1_parlay_positions.sql",
    "0021_v1_scorer_market.sql",
    "0022_v1_golf_markets.sql",
    "0023_v1_golf_context.sql",
    "0024_v1_golf_exclude_exchanges.sql",
    "0025_v2_golf_datagolf.sql",
    "0026_v2_golf_board_exclude_model.sql",
    "0027_v2_ingest_delete_injuries.sql",
    "0028_v2_market_odds.sql",
    "0029_v2_value_bets_markets.sql",
    "0030_v2_team_match_stats.sql",
    "0031_v2_golf_skill_ratings.sql",
    "0032_v2_golf_results.sql",
    "0033_v2_handicap_market.sql",
    "0034_v2_golf_catalog_scope.sql",
    "0035_v3_golf_pretournament_preds.sql",
    "0036_v3_golf_positions.sql",
    "0037_v3_position_ticket_line.sql",
    "0038_v4_auth_bankroll_rbac.sql",
]


def main() -> int:
    migrations_dir = Path("db/migrations")
    missing = [name for name in EXPECTED_FILES if not (migrations_dir / name).exists()]
    if missing:
        print("Missing migrations:", ", ".join(missing))
        return 1

    combined = "\n".join((migrations_dir / name).read_text(encoding="utf-8") for name in EXPECTED_FILES)
    checks = {
        "schemas": "CREATE SCHEMA IF NOT EXISTS core" in combined,
        "predictions_table": "CREATE TABLE IF NOT EXISTS model.predictions" in combined,
        "value_bets_table": "CREATE TABLE IF NOT EXISTS model.value_bets" in combined,
        "stake_provider": "provider_code = 'STAKE'" in combined or "'STAKE'" in combined,
        "theoddsapi_provider": "provider_code = 'THEODDSAPI'" in combined or "'THEODDSAPI'" in combined,
        "hardening_trigger": "ops.set_updated_at" in combined,
        "annotation_kind_scope": "uq_user_bet_annotations_recommendation" in combined,
        "real_taken_profit": "taken_profit_units" in combined,
        "position_tickets": "model.user_bet_positions" in combined,
        "parlay_tickets": "model.parlay_tickets" in combined,
        "scorer_market": "reporting.v_scorer_board" in combined,
        "golf_market": "reporting.v_golf_board" in combined
        and "core.golf_tournaments" in combined
        and "model.golf_predictions" in combined,
        "golf_context": "core.golf_tournament_context_factors" in combined
        and "venue_city" in combined,
        "datagolf": "core.golf_matchup_odds" in combined
        and "reporting.v_golf_matchup_board" in combined,
        "golf_skill_ratings": "core.golf_skill_ratings" in combined,
        "golf_results": "core.golf_results" in combined,
        "handicap_market": "'HANDICAP'" in combined and "model.value_bets ADD COLUMN IF NOT EXISTS line" in combined,
        "golf_catalog_scope": "catalog_status" in combined and "idx_golf_tournaments_scope" in combined,
        "golf_pretournament": "core.golf_pretournament_preds" in combined,
        "golf_positions": "golf_matchup_deal_id" in combined and "idx_user_bet_positions_golf_matchup" in combined,
        "position_line": "idx_user_positions_reco_line" in combined,
        "auth_users": "CREATE TABLE IF NOT EXISTS app_auth.users" in combined
        and "role_code IN ('CLIENT', 'ADMIN')" in combined,
        "auth_sessions": "CREATE TABLE IF NOT EXISTS app_auth.sessions" in combined
        and "token_hash text NOT NULL UNIQUE" in combined,
        "audit_log": "CREATE TABLE IF NOT EXISTS app_auth.audit_log" in combined
        and "idx_audit_actor_created" in combined,
        "bankroll_ledger": "CREATE TABLE IF NOT EXISTS model.user_bankroll_events" in combined
        and "'STAKE_PLACED'" in combined
        and "'BANKROLL_SET'" in combined,
        "position_user_scope": "ALTER TABLE model.user_bet_positions" in combined
        and "ADD COLUMN IF NOT EXISTS user_id" in combined
        and "idx_user_positions_user_reco" in combined,
    }

    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        print("Migration structure check failed:", ", ".join(failed))
        return 2

    print("Migration structure check succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
