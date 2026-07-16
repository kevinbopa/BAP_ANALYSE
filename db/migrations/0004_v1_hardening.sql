BEGIN;

CREATE OR REPLACE FUNCTION ops.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_ingestion_runs_finished_after_started'
    ) THEN
        ALTER TABLE ops.ingestion_runs
            ADD CONSTRAINT chk_ingestion_runs_finished_after_started
            CHECK (
                started_at IS NULL
                OR finished_at IS NULL
                OR finished_at >= started_at
            );
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_analysis_scopes_active_window'
    ) THEN
        ALTER TABLE core.analysis_scopes
            ADD CONSTRAINT chk_analysis_scopes_active_window
            CHECK (
                active_from IS NULL
                OR active_to IS NULL
                OR active_to >= active_from
            );
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_seasons_date_window'
    ) THEN
        ALTER TABLE core.seasons
            ADD CONSTRAINT chk_seasons_date_window
            CHECK (
                season_start_date IS NULL
                OR season_end_date IS NULL
                OR season_end_date >= season_start_date
            );
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_provider_payloads_checksum_not_blank'
    ) THEN
        ALTER TABLE raw.provider_payloads
            ADD CONSTRAINT chk_provider_payloads_checksum_not_blank
            CHECK (btrim(payload_checksum) <> '');
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_model_runs_finished_after_started'
    ) THEN
        ALTER TABLE model.model_runs
            ADD CONSTRAINT chk_model_runs_finished_after_started
            CHECK (
                started_at IS NULL
                OR finished_at IS NULL
                OR finished_at >= started_at
            );
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_model_runs_training_window'
    ) THEN
        ALTER TABLE model.model_runs
            ADD CONSTRAINT chk_model_runs_training_window
            CHECK (
                training_window_start IS NULL
                OR training_window_end IS NULL
                OR training_window_end >= training_window_start
            );
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_value_bets_status_code'
    ) THEN
        ALTER TABLE model.value_bets
            ADD CONSTRAINT chk_value_bets_status_code
            CHECK (status_code IN ('ACTIVE', 'SETTLED', 'VOID', 'EXPIRED', 'CANCELLED'));
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_value_bets_edge_range'
    ) THEN
        ALTER TABLE model.value_bets
            ADD CONSTRAINT chk_value_bets_edge_range
            CHECK (edge_probability > -1 AND edge_probability < 1);
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_seasons_one_current_per_league
    ON core.seasons (league_id)
    WHERE is_current;

CREATE UNIQUE INDEX IF NOT EXISTS uq_fixture_odds_one_closing_line_per_bookmaker
    ON core.fixture_odds_1x2 (fixture_id, bookmaker_id)
    WHERE is_closing_line;

CREATE INDEX IF NOT EXISTS idx_fixtures_home_team_kickoff
    ON core.fixtures (home_team_id, kickoff_utc DESC);

CREATE INDEX IF NOT EXISTS idx_fixtures_away_team_kickoff
    ON core.fixtures (away_team_id, kickoff_utc DESC);

CREATE INDEX IF NOT EXISTS idx_predictions_model_run
    ON model.predictions (model_run_id);

CREATE INDEX IF NOT EXISTS idx_value_bets_prediction
    ON model.value_bets (prediction_id);

CREATE INDEX IF NOT EXISTS idx_deal_rankings_fixture_generated
    ON model.deal_rankings (fixture_id, generated_at DESC);

CREATE INDEX IF NOT EXISTS idx_provider_endpoints_provider_domain
    ON ops.provider_endpoints (provider_id, entity_domain, is_enabled);

DROP TRIGGER IF EXISTS trg_set_updated_at_ops_providers ON ops.providers;
CREATE TRIGGER trg_set_updated_at_ops_providers
BEFORE UPDATE ON ops.providers
FOR EACH ROW
EXECUTE FUNCTION ops.set_updated_at();

DROP TRIGGER IF EXISTS trg_set_updated_at_core_analysis_scopes ON core.analysis_scopes;
CREATE TRIGGER trg_set_updated_at_core_analysis_scopes
BEFORE UPDATE ON core.analysis_scopes
FOR EACH ROW
EXECUTE FUNCTION ops.set_updated_at();

DROP TRIGGER IF EXISTS trg_set_updated_at_core_leagues ON core.leagues;
CREATE TRIGGER trg_set_updated_at_core_leagues
BEFORE UPDATE ON core.leagues
FOR EACH ROW
EXECUTE FUNCTION ops.set_updated_at();

DROP TRIGGER IF EXISTS trg_set_updated_at_core_seasons ON core.seasons;
CREATE TRIGGER trg_set_updated_at_core_seasons
BEFORE UPDATE ON core.seasons
FOR EACH ROW
EXECUTE FUNCTION ops.set_updated_at();

DROP TRIGGER IF EXISTS trg_set_updated_at_core_venues ON core.venues;
CREATE TRIGGER trg_set_updated_at_core_venues
BEFORE UPDATE ON core.venues
FOR EACH ROW
EXECUTE FUNCTION ops.set_updated_at();

DROP TRIGGER IF EXISTS trg_set_updated_at_core_teams ON core.teams;
CREATE TRIGGER trg_set_updated_at_core_teams
BEFORE UPDATE ON core.teams
FOR EACH ROW
EXECUTE FUNCTION ops.set_updated_at();

DROP TRIGGER IF EXISTS trg_set_updated_at_core_fixtures ON core.fixtures;
CREATE TRIGGER trg_set_updated_at_core_fixtures
BEFORE UPDATE ON core.fixtures
FOR EACH ROW
EXECUTE FUNCTION ops.set_updated_at();

DROP TRIGGER IF EXISTS trg_set_updated_at_core_fixture_scores ON core.fixture_scores;
CREATE TRIGGER trg_set_updated_at_core_fixture_scores
BEFORE UPDATE ON core.fixture_scores
FOR EACH ROW
EXECUTE FUNCTION ops.set_updated_at();

DROP TRIGGER IF EXISTS trg_set_updated_at_core_bookmakers ON core.bookmakers;
CREATE TRIGGER trg_set_updated_at_core_bookmakers
BEFORE UPDATE ON core.bookmakers
FOR EACH ROW
EXECUTE FUNCTION ops.set_updated_at();

REVOKE ALL ON ALL TABLES IN SCHEMA ops FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA raw FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA core FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA model FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA reporting FROM PUBLIC;

REVOKE ALL ON ALL SEQUENCES IN SCHEMA ops FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA raw FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA core FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA model FROM PUBLIC;

COMMENT ON FUNCTION ops.set_updated_at() IS
'Trigger utilitaire pour maintenir automatiquement les colonnes updated_at sur les tables metier.';

COMMIT;
