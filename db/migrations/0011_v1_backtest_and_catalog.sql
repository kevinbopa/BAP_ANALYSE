-- 0011 - Extension du catalogue de correlations + reglement des value bets.
--
-- 1. Nouveaux codes d'insight (familles ajoutees au moteur de correlations) :
--    COMP_SPLIT (performance par type de competition), TIER_SPLIT (vs niveau
--    d'adversaire), STREAK (apres grosse defaite), SCORING_PATTERN (profils
--    de buts : over 2.5, BTTS).
-- 2. Reglement des deals : chaque value bet recommande est regle des que son
--    match se termine -> track record ROI reel (le "backtest vivant", les
--    cotes historiques n'etant pas disponibles).

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Etendre les codes d'insight autorises
-- ---------------------------------------------------------------------------
ALTER TABLE model.correlation_insights
    DROP CONSTRAINT IF EXISTS correlation_insights_insight_code_check;
ALTER TABLE model.correlation_insights
    ADD CONSTRAINT correlation_insights_insight_code_check CHECK (insight_code IN (
        'PLAYER_PRESENCE',
        'SYNERGY_PAIR',
        'REST_IMPACT',
        'H2H',
        'COMP_SPLIT',
        'TIER_SPLIT',
        'STREAK',
        'SCORING_PATTERN',
        'CAPTAIN_EFFECT',
        'LINEUP_CLUSTER'
    ));

-- ---------------------------------------------------------------------------
-- 2. Reglement des value bets
-- ---------------------------------------------------------------------------
ALTER TABLE model.value_bets
    ADD COLUMN IF NOT EXISTS result_code text
        CHECK (result_code IN ('WON', 'LOST', 'VOID')),
    ADD COLUMN IF NOT EXISTS profit_units numeric(8, 4),
    ADD COLUMN IF NOT EXISTS settled_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_value_bets_unsettled
    ON model.value_bets (fixture_id) WHERE result_code IS NULL;

-- Vue de performance des deals regles (track record reel)
CREATE OR REPLACE VIEW reporting.v_deal_performance AS
SELECT
    COUNT(*) AS bets_settled,
    COUNT(*) FILTER (WHERE result_code = 'WON') AS wins,
    ROUND(AVG(CASE WHEN result_code = 'WON' THEN 1.0 ELSE 0.0 END) * 100, 1) AS win_rate_pct,
    ROUND(SUM(profit_units)::numeric, 2) AS total_profit_units,
    ROUND((SUM(profit_units) / NULLIF(COUNT(*), 0) * 100)::numeric, 2) AS roi_pct,
    ROUND(AVG(edge_probability)::numeric * 100, 2) AS avg_edge_pct
FROM model.value_bets
WHERE result_code IN ('WON', 'LOST');

GRANT SELECT ON reporting.v_deal_performance TO spe_app_rw, spe_readonly;

COMMIT;
