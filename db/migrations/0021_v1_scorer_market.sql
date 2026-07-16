-- 0021 : marche buteur (anytime goalscorer).
--
-- NOS probabilites (part de buts x xG equipe) confrontees aux cotes reelles
-- "player_goal_scorer_anytime" de The Odds API. Un buteur sans cote reste
-- une prediction ; avec cote et edge, il devient un deal.

BEGIN;

CREATE TABLE IF NOT EXISTS core.fixture_odds_scorer (
    fixture_odds_scorer_id bigserial PRIMARY KEY,
    fixture_id bigint NOT NULL REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    player_id bigint REFERENCES core.players(player_id) ON DELETE SET NULL,
    player_name_raw text NOT NULL,
    bookmaker_id bigint NOT NULL REFERENCES core.bookmakers(bookmaker_id) ON DELETE CASCADE,
    captured_at timestamptz NOT NULL,
    anytime_odd numeric(10, 4) NOT NULL,
    source_system text NOT NULL DEFAULT 'THEODDSAPI_V4',
    UNIQUE (fixture_id, player_name_raw, bookmaker_id, captured_at),
    CHECK (anytime_odd > 1.0000)
);

CREATE INDEX IF NOT EXISTS idx_odds_scorer_fixture
    ON core.fixture_odds_scorer (fixture_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS model.scorer_predictions (
    scorer_prediction_id bigserial PRIMARY KEY,
    fixture_id bigint NOT NULL REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    player_id bigint NOT NULL REFERENCES core.players(player_id) ON DELETE CASCADE,
    team_id bigint REFERENCES core.teams(team_id) ON DELETE SET NULL,
    probability numeric(8, 6) NOT NULL,
    method_code text NOT NULL DEFAULT 'GOAL_SHARE_POISSON',
    generated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (probability >= 0 AND probability <= 1)
);

CREATE INDEX IF NOT EXISTS idx_scorer_predictions_fixture
    ON model.scorer_predictions (fixture_id, generated_at DESC);

CREATE TABLE IF NOT EXISTS model.scorer_deals (
    scorer_deal_id bigserial PRIMARY KEY,
    fixture_id bigint NOT NULL REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    player_id bigint NOT NULL REFERENCES core.players(player_id) ON DELETE CASCADE,
    bookmaker_id bigint REFERENCES core.bookmakers(bookmaker_id) ON DELETE SET NULL,
    model_probability numeric(8, 6) NOT NULL,
    implied_probability numeric(8, 6) NOT NULL,
    edge_probability numeric(8, 6) NOT NULL,
    market_odd numeric(10, 4) NOT NULL,
    detected_at timestamptz NOT NULL DEFAULT now(),
    status_code text NOT NULL DEFAULT 'ACTIVE',
    result_code text,
    profit_units numeric(12, 4),
    settled_at timestamptz,
    CHECK (model_probability >= 0 AND model_probability <= 1),
    CHECK (market_odd > 1.0000),
    CHECK (status_code IN ('ACTIVE', 'CANCELLED')),
    CHECK (result_code IS NULL OR result_code IN ('WON', 'LOST', 'VOID'))
);

CREATE INDEX IF NOT EXISTS idx_scorer_deals_fixture
    ON model.scorer_deals (fixture_id, detected_at DESC);

-- Vue board : derniere prediction + meilleure cote par joueur/fixture.
CREATE OR REPLACE VIEW reporting.v_scorer_board AS
WITH latest_pred AS (
    SELECT DISTINCT ON (sp.fixture_id, sp.player_id)
        sp.fixture_id, sp.player_id, sp.team_id, sp.probability, sp.generated_at
    FROM model.scorer_predictions sp
    ORDER BY sp.fixture_id, sp.player_id, sp.generated_at DESC
),
best_odd AS (
    SELECT DISTINCT ON (os.fixture_id, os.player_id)
        os.fixture_id, os.player_id, os.anytime_odd, os.bookmaker_id
    FROM core.fixture_odds_scorer os
    WHERE os.player_id IS NOT NULL
    ORDER BY os.fixture_id, os.player_id, os.captured_at DESC, os.anytime_odd DESC
)
SELECT
    lp.fixture_id,
    lp.player_id,
    p.player_name,
    t.team_name,
    f.kickoff_utc,
    l.league_name,
    ht.team_name AS home_team_name,
    at.team_name AS away_team_name,
    lp.probability,
    bo.anytime_odd,
    b.bookmaker_name
FROM latest_pred lp
JOIN core.players p ON p.player_id = lp.player_id
JOIN core.fixtures f ON f.fixture_id = lp.fixture_id
JOIN core.leagues l ON l.league_id = f.league_id
JOIN core.teams ht ON ht.team_id = f.home_team_id
JOIN core.teams at ON at.team_id = f.away_team_id
LEFT JOIN core.teams t ON t.team_id = lp.team_id
LEFT JOIN best_odd bo ON bo.fixture_id = lp.fixture_id AND bo.player_id = lp.player_id
LEFT JOIN core.bookmakers b ON b.bookmaker_id = bo.bookmaker_id;

GRANT SELECT, INSERT, UPDATE ON core.fixture_odds_scorer TO spe_ingest_rw, spe_app_rw;
GRANT SELECT, INSERT, UPDATE ON model.scorer_predictions, model.scorer_deals TO spe_app_rw;
GRANT SELECT ON core.fixture_odds_scorer, model.scorer_predictions, model.scorer_deals,
    reporting.v_scorer_board TO spe_readonly;
GRANT SELECT ON reporting.v_scorer_board TO spe_app_rw, spe_ingest_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA core TO spe_ingest_rw, spe_app_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA model TO spe_app_rw;

COMMIT;
