from __future__ import annotations

from pathlib import Path
import re
import sys


MIGRATION_RE = re.compile(r"^(\d{4})_[a-zA-Z0-9_]+\.sql$")


def load_migration_files(migrations_dir: Path) -> list[Path]:
    files = sorted(migrations_dir.glob("*.sql"), key=lambda path: path.name)
    if not files:
        raise ValueError("No migration files found.")

    prefixes: list[int] = []
    invalid: list[str] = []
    for path in files:
        match = MIGRATION_RE.match(path.name)
        if not match:
            invalid.append(path.name)
            continue
        prefixes.append(int(match.group(1)))

    if invalid:
        raise ValueError("Invalid migration filenames: " + ", ".join(invalid))

    expected_prefixes = list(range(prefixes[0], prefixes[-1] + 1))
    if prefixes != expected_prefixes:
        raise ValueError(
            "Migration numbering is not contiguous: expected "
            + ", ".join(f"{value:04d}" for value in expected_prefixes)
        )

    return files


def main() -> int:
    migrations_dir = Path("db/migrations")
    try:
        migration_files = load_migration_files(migrations_dir)
    except ValueError as exc:
        print(str(exc))
        return 1

    combined = "\n".join(path.read_text(encoding="utf-8") for path in migration_files)
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
        "cashout_ledger": "cashout_amount" in combined
        and "cashed_out_at" in combined
        and "'CASHOUT'" in combined,
        "double_chance_fix": "value_bets_market_selection_chk" in combined
        and "DC_1X" in combined
        and "DC_X2" in combined,
        "ingestion_run_observability": "reporting.v_ingestion_run_latest" in combined
        and "heartbeat_at" in combined
        and "trigger_source" in combined
        and "application_name" in combined
        and "request_fingerprint" in combined,
        "client_account_snapshot": "reporting.v_user_account_snapshot" in combined
        and "open_total_stake" in combined
        and "active_session_count" in combined
        and "open_parlay_stake" in combined,
        "client_foundation": "CREATE TABLE IF NOT EXISTS app_auth.user_profiles" in combined
        and "CREATE TABLE IF NOT EXISTS app_auth.user_preferences" in combined
        and "CREATE TABLE IF NOT EXISTS app_auth.user_limits" in combined
        and "reporting.v_user_admin_profile" in combined,
        "payload_pipeline": "CREATE TABLE IF NOT EXISTS ops.provider_payload_normalizations" in combined
        and "reporting.v_provider_payload_pipeline" in combined
        and "payload_size_bytes" in combined
        and "entity_count" in combined,
    }

    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        print("Migration structure check failed:", ", ".join(failed))
        return 2

    print("Migration structure check succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
