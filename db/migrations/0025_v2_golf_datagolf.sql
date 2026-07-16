-- 0025 : Golf V2 — bascule sur DataGolf.
--
-- DataGolf couvre le circuit masculin complet (PGA/DP World/Korn Ferry/LIV)
-- avec un vrai modele strokes-gained + course-fit (endpoint pre-tournament),
-- les cotes books (dont bet365) pour outrights ET matchups, et le fair-odd
-- maison. On etend donc la couche golf V1 :
--   * identifiants DataGolf stables (dg_event_id, dg_id joueur, tour, course)
--   * le classement modele n'est plus un miroir des cotes mais le modele DG
--   * nouveau marche MATCHUPS (joueur A vs B) : la ou vit la value au golf
--
-- On garde provider_event_id comme cle naturelle : pour DataGolf on y stocke
-- une cle synthetique "<tour>-<season>-<dg_event_id>".

BEGIN;

-- --- Tournois : metadonnees DataGolf ----------------------------------------
ALTER TABLE core.golf_tournaments
    ADD COLUMN IF NOT EXISTS dg_event_id bigint,
    ADD COLUMN IF NOT EXISTS tour_code text,
    ADD COLUMN IF NOT EXISTS course_name text,
    ADD COLUMN IF NOT EXISTS date_start date,
    ADD COLUMN IF NOT EXISTS date_end date,
    ADD COLUMN IF NOT EXISTS current_round integer,
    ADD COLUMN IF NOT EXISTS season integer;

CREATE INDEX IF NOT EXISTS idx_golf_tournaments_dg
    ON core.golf_tournaments (tour_code, dg_event_id, season);

-- --- Joueurs : identite DataGolf stable --------------------------------------
ALTER TABLE core.golf_players
    ADD COLUMN IF NOT EXISTS dg_id bigint,
    ADD COLUMN IF NOT EXISTS owgr_rank integer,
    ADD COLUMN IF NOT EXISTS dg_rank integer,
    ADD COLUMN IF NOT EXISTS amateur boolean NOT NULL DEFAULT false;

CREATE UNIQUE INDEX IF NOT EXISTS uq_golf_players_dg_id
    ON core.golf_players (dg_id) WHERE dg_id IS NOT NULL;

-- --- Matchups : cotes brutes (books + fair DataGolf via predictions) ---------
CREATE TABLE IF NOT EXISTS core.golf_matchup_odds (
    golf_matchup_odd_id bigserial PRIMARY KEY,
    golf_tournament_id bigint NOT NULL REFERENCES core.golf_tournaments(golf_tournament_id) ON DELETE CASCADE,
    market_code text NOT NULL,
    bookmaker_id bigint NOT NULL REFERENCES core.bookmakers(bookmaker_id) ON DELETE CASCADE,
    p1_golf_player_id bigint NOT NULL REFERENCES core.golf_players(golf_player_id) ON DELETE CASCADE,
    p2_golf_player_id bigint NOT NULL REFERENCES core.golf_players(golf_player_id) ON DELETE CASCADE,
    p3_golf_player_id bigint REFERENCES core.golf_players(golf_player_id) ON DELETE CASCADE,
    p1_odd numeric(10, 4),
    p2_odd numeric(10, 4),
    p3_odd numeric(10, 4),
    tie_odd numeric(10, 4),
    ties_rule text,
    captured_at timestamptz NOT NULL,
    source_system text NOT NULL DEFAULT 'DATAGOLF_V1',
    raw_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (golf_tournament_id, market_code, bookmaker_id,
            p1_golf_player_id, p2_golf_player_id, captured_at),
    CHECK (market_code IN ('TOURNAMENT_MATCHUP', 'ROUND_MATCHUP', 'THREE_BALL'))
);

CREATE INDEX IF NOT EXISTS idx_golf_matchup_odds_tournament
    ON core.golf_matchup_odds (golf_tournament_id, market_code, captured_at DESC);

-- --- Matchups : probabilites du modele DataGolf ------------------------------
CREATE TABLE IF NOT EXISTS model.golf_matchup_predictions (
    golf_matchup_prediction_id bigserial PRIMARY KEY,
    golf_tournament_id bigint NOT NULL REFERENCES core.golf_tournaments(golf_tournament_id) ON DELETE CASCADE,
    market_code text NOT NULL,
    p1_golf_player_id bigint NOT NULL REFERENCES core.golf_players(golf_player_id) ON DELETE CASCADE,
    p2_golf_player_id bigint NOT NULL REFERENCES core.golf_players(golf_player_id) ON DELETE CASCADE,
    p3_golf_player_id bigint REFERENCES core.golf_players(golf_player_id) ON DELETE CASCADE,
    p1_probability numeric(8, 6),
    p2_probability numeric(8, 6),
    p3_probability numeric(8, 6),
    tie_probability numeric(8, 6),
    method_code text NOT NULL DEFAULT 'GOLF_DATAGOLF_MATCHUP_V1',
    generated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (market_code IN ('TOURNAMENT_MATCHUP', 'ROUND_MATCHUP', 'THREE_BALL'))
);

CREATE INDEX IF NOT EXISTS idx_golf_matchup_predictions_latest
    ON model.golf_matchup_predictions (golf_tournament_id, market_code, generated_at DESC);

-- --- Matchups : deals value (on backe un cote precis) ------------------------
CREATE TABLE IF NOT EXISTS model.golf_matchup_deals (
    golf_matchup_deal_id bigserial PRIMARY KEY,
    golf_tournament_id bigint NOT NULL REFERENCES core.golf_tournaments(golf_tournament_id) ON DELETE CASCADE,
    market_code text NOT NULL,
    bookmaker_id bigint REFERENCES core.bookmakers(bookmaker_id) ON DELETE SET NULL,
    pick_golf_player_id bigint NOT NULL REFERENCES core.golf_players(golf_player_id) ON DELETE CASCADE,
    opponent_golf_player_id bigint NOT NULL REFERENCES core.golf_players(golf_player_id) ON DELETE CASCADE,
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
    CHECK (result_code IS NULL OR result_code IN ('WON', 'LOST', 'VOID', 'PUSH'))
);

CREATE INDEX IF NOT EXISTS idx_golf_matchup_deals_tournament
    ON model.golf_matchup_deals (golf_tournament_id, detected_at DESC);

-- --- Board golf : exposer tour / course / dates ------------------------------
-- On ajoute des colonnes au milieu -> CREATE OR REPLACE refuse, on recree.
DROP VIEW IF EXISTS reporting.v_golf_board;
CREATE VIEW reporting.v_golf_board AS
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
    JOIN core.bookmakers b ON b.bookmaker_id = go.bookmaker_id
    WHERE b.bookmaker_code !~ '_EX_'
      AND b.bookmaker_code NOT IN ('MATCHBOOK', 'SMARKETS', 'BETDAQ')
    ORDER BY go.golf_tournament_id, go.market_code, go.selection_name, go.bookmaker_id, go.captured_at DESC
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
    SELECT golf_tournament_context_factors.golf_tournament_id,
        jsonb_object_agg(
            golf_tournament_context_factors.factor_code,
            jsonb_build_object(
                'value', golf_tournament_context_factors.factor_value,
                'weight', golf_tournament_context_factors.weight,
                'source', golf_tournament_context_factors.source_code,
                'note', golf_tournament_context_factors.note
            )
        ) AS context_json
    FROM core.golf_tournament_context_factors
    GROUP BY golf_tournament_context_factors.golf_tournament_id
)
SELECT gt.golf_tournament_id,
    gt.provider_event_id,
    gt.sport_key,
    gt.tournament_name,
    gt.sport_title,
    gt.tour_code,
    gt.course_name,
    gt.date_start,
    gt.date_end,
    gt.current_round,
    gt.commence_time,
    gt.venue_name,
    gt.venue_city,
    gt.venue_country,
    context.context_json,
    lp.golf_player_id,
    COALESCE(p.player_name, lp.selection_name, bo.selection_name) AS player_name,
    p.country_code,
    p.owgr_rank,
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
    LEFT JOIN best_odds bo ON bo.golf_tournament_id = gt.golf_tournament_id
        AND bo.market_code = lp.market_code
        AND bo.selection_name = lp.selection_name
    LEFT JOIN core.bookmakers b ON b.bookmaker_id = bo.bookmaker_id
    LEFT JOIN context ON context.golf_tournament_id = gt.golf_tournament_id
    LEFT JOIN model.golf_deals gd ON gd.golf_tournament_id = gt.golf_tournament_id
        AND gd.market_code = lp.market_code
        AND gd.selection_name = lp.selection_name
        AND gd.status_code = 'ACTIVE'::text
        AND gd.result_code IS NULL;

-- --- Board matchups : proba modele + meilleure cote book par cote du duel ----
CREATE OR REPLACE VIEW reporting.v_golf_matchup_board AS
WITH latest_pred AS (
    SELECT DISTINCT ON (mp.golf_tournament_id, mp.market_code, mp.p1_golf_player_id, mp.p2_golf_player_id)
        mp.*
    FROM model.golf_matchup_predictions mp
    ORDER BY mp.golf_tournament_id, mp.market_code, mp.p1_golf_player_id, mp.p2_golf_player_id, mp.generated_at DESC
)
SELECT gt.golf_tournament_id,
    gt.tournament_name,
    gt.tour_code,
    lp.market_code,
    lp.p1_golf_player_id,
    p1.player_name AS p1_name,
    lp.p2_golf_player_id,
    p2.player_name AS p2_name,
    lp.p1_probability,
    lp.p2_probability,
    lp.tie_probability,
    lp.generated_at,
    d.pick_golf_player_id,
    d.market_odd AS deal_odd,
    d.edge_probability AS deal_edge,
    d.bookmaker_id AS deal_bookmaker_id,
    db.bookmaker_name AS deal_bookmaker_name
FROM latest_pred lp
    JOIN core.golf_tournaments gt ON gt.golf_tournament_id = lp.golf_tournament_id
    JOIN core.golf_players p1 ON p1.golf_player_id = lp.p1_golf_player_id
    JOIN core.golf_players p2 ON p2.golf_player_id = lp.p2_golf_player_id
    LEFT JOIN model.golf_matchup_deals d ON d.golf_tournament_id = lp.golf_tournament_id
        AND d.market_code = lp.market_code
        AND ((d.pick_golf_player_id = lp.p1_golf_player_id AND d.opponent_golf_player_id = lp.p2_golf_player_id)
          OR (d.pick_golf_player_id = lp.p2_golf_player_id AND d.opponent_golf_player_id = lp.p1_golf_player_id))
        AND d.status_code = 'ACTIVE' AND d.result_code IS NULL
    LEFT JOIN core.bookmakers db ON db.bookmaker_id = d.bookmaker_id;

-- --- Droits -----------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON core.golf_matchup_odds TO spe_ingest_rw, spe_app_rw;
GRANT SELECT, INSERT, UPDATE ON model.golf_matchup_predictions, model.golf_matchup_deals TO spe_app_rw;
GRANT SELECT ON core.golf_matchup_odds, model.golf_matchup_predictions,
    model.golf_matchup_deals, reporting.v_golf_matchup_board TO spe_readonly;
GRANT SELECT ON reporting.v_golf_board, reporting.v_golf_matchup_board TO spe_app_rw, spe_ingest_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA core TO spe_ingest_rw, spe_app_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA model TO spe_app_rw;

COMMIT;
