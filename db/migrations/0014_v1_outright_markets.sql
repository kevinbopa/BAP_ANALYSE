-- 0014 : marches long terme ("faits divers") — vainqueur de championnat,
-- vainqueur de tournoi, meilleur buteur, indice Ballon d'Or.
--
-- Meme philosophie que les matchs : model.outright_predictions porte NOS
-- probabilites, core.outright_odds porte les prix du marche, et
-- model.outright_deals ne nait que d'un edge — un fait divers sans cote
-- reste une prediction, jamais un deal.

BEGIN;

CREATE TABLE IF NOT EXISTS core.outright_markets (
    outright_market_id bigserial PRIMARY KEY,
    market_code text NOT NULL,
    league_id bigint REFERENCES core.leagues(league_id) ON DELETE CASCADE,
    season_name text,
    market_label text NOT NULL,
    deadline_utc timestamptz,
    status_code text NOT NULL DEFAULT 'OPEN',
    created_at timestamptz NOT NULL DEFAULT now(),
    -- NULLS NOT DISTINCT : le Ballon d'Or n'a pas de ligue (league_id NULL)
    -- et doit quand meme s'upserter proprement.
    UNIQUE NULLS NOT DISTINCT (market_code, league_id, season_name),
    CHECK (market_code IN ('LEAGUE_WINNER', 'TOURNAMENT_WINNER', 'TOP_SCORER', 'BALLON_DOR_INDEX')),
    CHECK (status_code IN ('OPEN', 'SETTLED', 'CANCELLED'))
);

CREATE TABLE IF NOT EXISTS core.outright_selections (
    outright_selection_id bigserial PRIMARY KEY,
    outright_market_id bigint NOT NULL REFERENCES core.outright_markets(outright_market_id) ON DELETE CASCADE,
    subject_type text NOT NULL,
    team_id bigint REFERENCES core.teams(team_id) ON DELETE CASCADE,
    player_id bigint REFERENCES core.players(player_id) ON DELETE CASCADE,
    subject_label text NOT NULL,
    is_winner boolean,
    UNIQUE (outright_market_id, subject_label),
    CHECK (subject_type IN ('TEAM', 'PLAYER')),
    CHECK (
        (subject_type = 'TEAM' AND team_id IS NOT NULL)
        OR (subject_type = 'PLAYER')
    )
);

CREATE TABLE IF NOT EXISTS core.outright_odds (
    outright_odd_id bigserial PRIMARY KEY,
    outright_selection_id bigint NOT NULL REFERENCES core.outright_selections(outright_selection_id) ON DELETE CASCADE,
    bookmaker_id bigint NOT NULL REFERENCES core.bookmakers(bookmaker_id) ON DELETE CASCADE,
    captured_at timestamptz NOT NULL,
    decimal_odd numeric(10, 4) NOT NULL,
    source_system text NOT NULL DEFAULT 'THEODDSAPI_V4',
    source_reference text,
    UNIQUE (outright_selection_id, bookmaker_id, captured_at),
    CHECK (decimal_odd > 1.0000)
);

CREATE INDEX IF NOT EXISTS idx_outright_odds_selection
    ON core.outright_odds (outright_selection_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS model.outright_predictions (
    outright_prediction_id bigserial PRIMARY KEY,
    outright_market_id bigint NOT NULL REFERENCES core.outright_markets(outright_market_id) ON DELETE CASCADE,
    outright_selection_id bigint NOT NULL REFERENCES core.outright_selections(outright_selection_id) ON DELETE CASCADE,
    probability numeric(8, 6) NOT NULL,
    method_code text NOT NULL,
    details_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    generated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (probability >= 0 AND probability <= 1),
    CHECK (method_code IN ('SEASON_SIMULATION', 'KNOCKOUT_SIMULATION', 'SCORER_PROJECTION', 'PERFORMANCE_INDEX'))
);

CREATE INDEX IF NOT EXISTS idx_outright_predictions_market_generated
    ON model.outright_predictions (outright_market_id, generated_at DESC);

CREATE TABLE IF NOT EXISTS model.outright_deals (
    outright_deal_id bigserial PRIMARY KEY,
    outright_market_id bigint NOT NULL REFERENCES core.outright_markets(outright_market_id) ON DELETE CASCADE,
    outright_selection_id bigint NOT NULL REFERENCES core.outright_selections(outright_selection_id) ON DELETE CASCADE,
    bookmaker_id bigint REFERENCES core.bookmakers(bookmaker_id) ON DELETE SET NULL,
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

CREATE INDEX IF NOT EXISTS idx_outright_deals_market
    ON model.outright_deals (outright_market_id, detected_at DESC);

-- Vue : dernier etat de chaque marche (top predictions + meilleure cote).
CREATE OR REPLACE VIEW reporting.v_outright_board AS
WITH latest_predictions AS (
    SELECT DISTINCT ON (op.outright_market_id, op.outright_selection_id)
        op.outright_market_id,
        op.outright_selection_id,
        op.probability,
        op.method_code,
        op.generated_at
    FROM model.outright_predictions op
    ORDER BY op.outright_market_id, op.outright_selection_id, op.generated_at DESC
),
best_odds AS (
    SELECT DISTINCT ON (oo.outright_selection_id)
        oo.outright_selection_id,
        oo.decimal_odd,
        oo.bookmaker_id,
        oo.captured_at
    FROM core.outright_odds oo
    ORDER BY oo.outright_selection_id, oo.captured_at DESC, oo.decimal_odd DESC
)
SELECT
    m.outright_market_id,
    m.market_code,
    m.market_label,
    m.season_name,
    m.deadline_utc,
    m.status_code,
    l.league_name,
    s.outright_selection_id,
    s.subject_type,
    s.subject_label,
    lp.probability,
    lp.method_code,
    lp.generated_at,
    bo.decimal_odd AS best_odd,
    b.bookmaker_name
FROM core.outright_markets m
JOIN core.outright_selections s ON s.outright_market_id = m.outright_market_id
LEFT JOIN core.leagues l ON l.league_id = m.league_id
LEFT JOIN latest_predictions lp
    ON lp.outright_market_id = m.outright_market_id
   AND lp.outright_selection_id = s.outright_selection_id
LEFT JOIN best_odds bo ON bo.outright_selection_id = s.outright_selection_id
LEFT JOIN core.bookmakers b ON b.bookmaker_id = bo.bookmaker_id;

GRANT SELECT, INSERT, UPDATE ON core.outright_markets, core.outright_selections, core.outright_odds
    TO spe_ingest_rw, spe_app_rw;
GRANT SELECT, INSERT, UPDATE ON model.outright_predictions, model.outright_deals TO spe_app_rw;
GRANT SELECT ON core.outright_markets, core.outright_selections, core.outright_odds,
    model.outright_predictions, model.outright_deals, reporting.v_outright_board TO spe_readonly;
GRANT SELECT ON reporting.v_outright_board TO spe_app_rw, spe_ingest_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA core TO spe_ingest_rw, spe_app_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA model TO spe_app_rw;

COMMIT;
