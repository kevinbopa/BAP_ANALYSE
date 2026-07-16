-- 0023 : contexte Golf V1.
--
-- Le golf depend fortement du parcours et des conditions. Cette migration
-- ajoute les champs de lieu et une table de facteurs par tournoi pour
-- brancher meteo, force du champ, fit parcours et signaux externes sans
-- melanger ces donnees avec les cotes.

BEGIN;

ALTER TABLE core.golf_tournaments
    ADD COLUMN IF NOT EXISTS venue_name text,
    ADD COLUMN IF NOT EXISTS venue_city text,
    ADD COLUMN IF NOT EXISTS venue_country text,
    ADD COLUMN IF NOT EXISTS venue_lat numeric(9, 6),
    ADD COLUMN IF NOT EXISTS venue_lon numeric(9, 6);

CREATE TABLE IF NOT EXISTS core.golf_tournament_context_factors (
    golf_tournament_context_factor_id bigserial PRIMARY KEY,
    golf_tournament_id bigint NOT NULL REFERENCES core.golf_tournaments(golf_tournament_id) ON DELETE CASCADE,
    factor_code text NOT NULL,
    factor_value numeric(8, 4) NOT NULL,
    weight numeric(6, 3) NOT NULL DEFAULT 1.0,
    source_code text NOT NULL,
    note text,
    raw_context_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (golf_tournament_id, factor_code, source_code),
    CHECK (factor_code IN ('WEATHER', 'COURSE_FIT', 'FIELD_STRENGTH', 'PLAYER_FORM', 'NEWS')),
    CHECK (weight >= 0)
);

CREATE INDEX IF NOT EXISTS idx_golf_context_tournament
    ON core.golf_tournament_context_factors (golf_tournament_id, factor_code);

DROP VIEW IF EXISTS reporting.v_golf_board;
CREATE OR REPLACE VIEW reporting.v_golf_board AS
WITH latest_predictions AS (
    SELECT DISTINCT ON (gp.golf_tournament_id, gp.market_code, gp.selection_name)
        gp.golf_tournament_id,
        gp.golf_player_id,
        gp.market_code,
        gp.selection_name,
        gp.model_probability,
        gp.model_fair_odd,
        gp.method_code,
        gp.feature_snapshot_json,
        gp.generated_at
    FROM model.golf_predictions gp
    ORDER BY gp.golf_tournament_id, gp.market_code, gp.selection_name, gp.generated_at DESC
),
latest_odds AS (
    SELECT DISTINCT ON (go.golf_tournament_id, go.market_code, go.selection_name, go.bookmaker_id)
        go.golf_tournament_id,
        go.golf_player_id,
        go.bookmaker_id,
        go.market_code,
        go.selection_name,
        go.decimal_odd,
        go.captured_at
    FROM core.golf_odds go
    ORDER BY go.golf_tournament_id, go.market_code, go.selection_name, go.bookmaker_id,
             go.captured_at DESC
),
best_odds AS (
    SELECT DISTINCT ON (lo.golf_tournament_id, lo.market_code, lo.selection_name)
        lo.golf_tournament_id,
        lo.golf_player_id,
        lo.bookmaker_id,
        lo.market_code,
        lo.selection_name,
        lo.decimal_odd,
        lo.captured_at
    FROM latest_odds lo
    ORDER BY lo.golf_tournament_id, lo.market_code, lo.selection_name, lo.decimal_odd DESC
),
context AS (
    SELECT
        golf_tournament_id,
        jsonb_object_agg(factor_code, jsonb_build_object(
            'value', factor_value,
            'weight', weight,
            'source', source_code,
            'note', note
        )) AS context_json
    FROM core.golf_tournament_context_factors
    GROUP BY golf_tournament_id
)
SELECT
    gt.golf_tournament_id,
    gt.provider_event_id,
    gt.sport_key,
    gt.tournament_name,
    gt.sport_title,
    gt.commence_time,
    gt.venue_name,
    gt.venue_city,
    gt.venue_country,
    context.context_json,
    lp.golf_player_id,
    COALESCE(p.player_name, lp.selection_name, bo.selection_name) AS player_name,
    COALESCE(lp.market_code, bo.market_code) AS market_code,
    lp.model_probability,
    lp.model_fair_odd,
    lp.method_code,
    lp.feature_snapshot_json,
    lp.generated_at,
    bo.decimal_odd AS best_odd,
    bo.captured_at AS odd_captured_at,
    b.bookmaker_name,
    gd.edge_probability,
    gd.market_odd AS deal_odd,
    gd.detected_at AS deal_detected_at
FROM core.golf_tournaments gt
LEFT JOIN latest_predictions lp ON lp.golf_tournament_id = gt.golf_tournament_id
LEFT JOIN core.golf_players p ON p.golf_player_id = lp.golf_player_id
LEFT JOIN best_odds bo
    ON bo.golf_tournament_id = gt.golf_tournament_id
   AND bo.market_code = lp.market_code
   AND bo.selection_name = lp.selection_name
LEFT JOIN core.bookmakers b ON b.bookmaker_id = bo.bookmaker_id
LEFT JOIN context ON context.golf_tournament_id = gt.golf_tournament_id
LEFT JOIN model.golf_deals gd
    ON gd.golf_tournament_id = gt.golf_tournament_id
   AND gd.market_code = lp.market_code
   AND gd.selection_name = lp.selection_name
   AND gd.status_code = 'ACTIVE'
   AND gd.result_code IS NULL;

GRANT SELECT, INSERT, UPDATE ON core.golf_tournament_context_factors TO spe_ingest_rw, spe_app_rw;
GRANT SELECT ON core.golf_tournament_context_factors, reporting.v_golf_board TO spe_readonly;
GRANT SELECT ON reporting.v_golf_board TO spe_app_rw, spe_ingest_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA core TO spe_ingest_rw, spe_app_rw;

COMMIT;
