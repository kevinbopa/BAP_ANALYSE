-- 0022 : couche Golf V1.
--
-- Objectif produit : ajouter le golf sans polluer les tables football.
-- core.golf_* stocke les tournois, joueurs et cotes reels; model.golf_*
-- stocke nos probabilites et deals. La V1 commence par les outrights
-- (vainqueur de tournoi), puis accepte deja les marches top finish/rounds
-- si The Odds API les expose.

BEGIN;

CREATE TABLE IF NOT EXISTS core.golf_tournaments (
    golf_tournament_id bigserial PRIMARY KEY,
    provider_event_id text NOT NULL UNIQUE,
    sport_key text NOT NULL,
    tournament_name text NOT NULL,
    sport_title text,
    commence_time timestamptz,
    completed_at timestamptz,
    raw_event_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (length(provider_event_id) > 0),
    CHECK (length(tournament_name) > 0)
);

CREATE INDEX IF NOT EXISTS idx_golf_tournaments_commence
    ON core.golf_tournaments (commence_time DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS core.golf_players (
    golf_player_id bigserial PRIMARY KEY,
    player_name text NOT NULL,
    provider_participant_id text,
    country_code text,
    raw_player_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (player_name, provider_participant_id),
    CHECK (length(player_name) > 0)
);

CREATE TABLE IF NOT EXISTS core.golf_odds (
    golf_odd_id bigserial PRIMARY KEY,
    golf_tournament_id bigint NOT NULL REFERENCES core.golf_tournaments(golf_tournament_id) ON DELETE CASCADE,
    golf_player_id bigint REFERENCES core.golf_players(golf_player_id) ON DELETE SET NULL,
    bookmaker_id bigint NOT NULL REFERENCES core.bookmakers(bookmaker_id) ON DELETE CASCADE,
    market_code text NOT NULL,
    selection_name text NOT NULL,
    captured_at timestamptz NOT NULL,
    decimal_odd numeric(10, 4) NOT NULL,
    source_system text NOT NULL DEFAULT 'THEODDSAPI_V4',
    source_reference text,
    raw_market_key text,
    raw_outcome_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (golf_tournament_id, bookmaker_id, market_code, selection_name, captured_at),
    CHECK (decimal_odd > 1.0000),
    CHECK (market_code IN (
        'TOURNAMENT_WINNER',
        'TOP_3',
        'TOP_5',
        'TOP_10',
        'TOP_20',
        'ROUND_WINNER',
        'MAKE_CUT',
        'POSITION_FINISH'
    ))
);

CREATE INDEX IF NOT EXISTS idx_golf_odds_tournament_market
    ON core.golf_odds (golf_tournament_id, market_code, captured_at DESC);

CREATE TABLE IF NOT EXISTS model.golf_predictions (
    golf_prediction_id bigserial PRIMARY KEY,
    golf_tournament_id bigint NOT NULL REFERENCES core.golf_tournaments(golf_tournament_id) ON DELETE CASCADE,
    golf_player_id bigint REFERENCES core.golf_players(golf_player_id) ON DELETE SET NULL,
    market_code text NOT NULL,
    selection_name text NOT NULL,
    model_probability numeric(8, 6) NOT NULL,
    model_fair_odd numeric(10, 4),
    method_code text NOT NULL DEFAULT 'GOLF_MARKET_CONSENSUS_V1',
    feature_snapshot_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    generated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (model_probability >= 0 AND model_probability <= 1),
    CHECK (model_fair_odd IS NULL OR model_fair_odd > 1.0000),
    CHECK (market_code IN (
        'TOURNAMENT_WINNER',
        'TOP_3',
        'TOP_5',
        'TOP_10',
        'TOP_20',
        'ROUND_WINNER',
        'MAKE_CUT',
        'POSITION_FINISH'
    ))
);

CREATE INDEX IF NOT EXISTS idx_golf_predictions_latest
    ON model.golf_predictions (golf_tournament_id, market_code, generated_at DESC);

CREATE TABLE IF NOT EXISTS model.golf_deals (
    golf_deal_id bigserial PRIMARY KEY,
    golf_tournament_id bigint NOT NULL REFERENCES core.golf_tournaments(golf_tournament_id) ON DELETE CASCADE,
    golf_player_id bigint REFERENCES core.golf_players(golf_player_id) ON DELETE SET NULL,
    bookmaker_id bigint REFERENCES core.bookmakers(bookmaker_id) ON DELETE SET NULL,
    market_code text NOT NULL,
    selection_name text NOT NULL,
    model_probability numeric(8, 6) NOT NULL,
    implied_probability numeric(8, 6) NOT NULL,
    edge_probability numeric(8, 6) NOT NULL,
    market_odd numeric(10, 4) NOT NULL,
    detected_at timestamptz NOT NULL DEFAULT now(),
    status_code text NOT NULL DEFAULT 'ACTIVE',
    result_code text,
    profit_units numeric(10, 4),
    settled_at timestamptz,
    CHECK (model_probability >= 0 AND model_probability <= 1),
    CHECK (implied_probability >= 0 AND implied_probability <= 1),
    CHECK (market_odd > 1.0000),
    CHECK (status_code IN ('ACTIVE', 'CANCELLED')),
    CHECK (result_code IS NULL OR result_code IN ('WON', 'LOST', 'VOID'))
);

CREATE INDEX IF NOT EXISTS idx_golf_deals_tournament
    ON model.golf_deals (golf_tournament_id, detected_at DESC);

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
)
SELECT
    gt.golf_tournament_id,
    gt.provider_event_id,
    gt.sport_key,
    gt.tournament_name,
    gt.sport_title,
    gt.commence_time,
    lp.golf_player_id,
    COALESCE(p.player_name, lp.selection_name, bo.selection_name) AS player_name,
    COALESCE(lp.market_code, bo.market_code) AS market_code,
    lp.model_probability,
    lp.model_fair_odd,
    lp.method_code,
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
LEFT JOIN model.golf_deals gd
    ON gd.golf_tournament_id = gt.golf_tournament_id
   AND gd.market_code = lp.market_code
   AND gd.selection_name = lp.selection_name
   AND gd.status_code = 'ACTIVE'
   AND gd.result_code IS NULL;

GRANT SELECT, INSERT, UPDATE ON core.golf_tournaments, core.golf_players, core.golf_odds
    TO spe_ingest_rw, spe_app_rw;
GRANT SELECT, INSERT, UPDATE ON model.golf_predictions, model.golf_deals TO spe_app_rw;
GRANT SELECT ON core.golf_tournaments, core.golf_players, core.golf_odds,
    model.golf_predictions, model.golf_deals, reporting.v_golf_board TO spe_readonly;
GRANT SELECT ON reporting.v_golf_board TO spe_app_rw, spe_ingest_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA core TO spe_ingest_rw, spe_app_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA model TO spe_app_rw;

COMMIT;
