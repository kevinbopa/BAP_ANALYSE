-- 0019 : carnet de positions multiples + marches pronostic libres.
--
-- model.user_bet_annotations reste la note privee par recommandation.
-- model.user_bet_positions devient le journal reel des prises : chaque clic OK
-- cree un ticket, ce qui permet de prendre plusieurs fois la meme position.

BEGIN;

CREATE TABLE IF NOT EXISTS model.user_bet_positions (
    position_id bigserial PRIMARY KEY,
    annotation_id bigint REFERENCES model.user_bet_annotations(annotation_id) ON DELETE SET NULL,
    bet_kind text NOT NULL,
    fixture_id bigint REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    outright_market_id bigint REFERENCES core.outright_markets(outright_market_id) ON DELETE CASCADE,
    market_code text NOT NULL,
    selection_code text NOT NULL,
    selection_label text,
    model_probability numeric(10, 8),
    model_fair_odd numeric(10, 4),
    taken_odd numeric(10, 4) NOT NULL,
    stake_amount numeric(12, 4) NOT NULL DEFAULT 1,
    taken_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (bet_kind IN ('DEAL', 'PRONOSTIC', 'OUTRIGHT')),
    CHECK (fixture_id IS NOT NULL OR outright_market_id IS NOT NULL),
    CHECK (taken_odd > 1),
    CHECK (stake_amount > 0),
    CHECK (model_probability IS NULL OR (model_probability >= 0 AND model_probability <= 1)),
    CHECK (model_fair_odd IS NULL OR model_fair_odd > 1)
);

CREATE INDEX IF NOT EXISTS idx_user_positions_reco
    ON model.user_bet_positions (
        bet_kind, fixture_id, outright_market_id, market_code, selection_code
    );

CREATE INDEX IF NOT EXISTS idx_user_positions_taken_at
    ON model.user_bet_positions (taken_at DESC);

-- Backfill des anciennes prises uniques vers le nouveau journal.
INSERT INTO model.user_bet_positions (
    annotation_id, bet_kind, fixture_id, outright_market_id, market_code,
    selection_code, taken_odd, stake_amount, taken_at
)
SELECT
    a.annotation_id, a.bet_kind, a.fixture_id, a.outright_market_id, a.market_code,
    a.selection_code, a.taken_odd, COALESCE(a.stake_amount, 1), COALESCE(a.taken_at, a.updated_at, now())
FROM model.user_bet_annotations a
WHERE COALESCE(a.taken, false) = true
  AND a.taken_odd IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM model.user_bet_positions p
      WHERE p.annotation_id = a.annotation_id
  );

DROP VIEW IF EXISTS reporting.v_track_record;

CREATE VIEW reporting.v_track_record AS
WITH position_rollup AS (
    SELECT
        bet_kind,
        fixture_id,
        outright_market_id,
        market_code,
        selection_code,
        MAX(selection_label) AS selection_label,
        COUNT(*)::integer AS position_count,
        true AS taken,
        ROUND((SUM(stake_amount * taken_odd) / NULLIF(SUM(stake_amount), 0))::numeric, 4) AS taken_odd,
        ROUND(SUM(stake_amount)::numeric, 4) AS stake_amount,
        MIN(taken_at) AS taken_at,
        MAX(model_probability) AS model_probability,
        MAX(model_fair_odd) AS model_fair_odd
    FROM model.user_bet_positions
    GROUP BY bet_kind, fixture_id, outright_market_id, market_code, selection_code
),
latest_prediction AS (
    SELECT DISTINCT ON (p.fixture_id)
        p.fixture_id,
        p.prediction_id,
        p.home_win_probability,
        p.draw_probability,
        p.away_win_probability,
        p.generated_at
    FROM model.predictions p
    ORDER BY p.fixture_id, p.generated_at DESC, p.prediction_id DESC
)
SELECT
    'DEAL' AS bet_kind,
    vb.fixture_id,
    NULL::bigint AS outright_market_id,
    l.league_name,
    ht.team_name AS home_team_name,
    at.team_name AS away_team_name,
    f.kickoff_utc,
    vb.market_code,
    vb.selection_code,
    NULL::text AS subject_label,
    vb.model_probability,
    vb.fair_odd AS model_fair_odd,
    vb.market_odd,
    vb.edge_probability,
    vb.result_code,
    vb.profit_units,
    vb.settled_at,
    COALESCE(pr.taken, false) AS taken,
    COALESCE(pr.position_count, 0) AS position_count,
    pr.taken_odd,
    pr.stake_amount,
    pr.taken_at,
    CASE
        WHEN COALESCE(pr.taken, false) = false THEN NULL::numeric
        WHEN vb.result_code = 'WON' AND pr.taken_odd IS NOT NULL
            THEN ROUND((COALESCE(pr.stake_amount, 1) * (pr.taken_odd - 1))::numeric, 4)
        WHEN vb.result_code = 'LOST'
            THEN ROUND((-COALESCE(pr.stake_amount, 1))::numeric, 4)
        WHEN vb.result_code = 'VOID'
            THEN 0::numeric
        ELSE NULL::numeric
    END AS taken_profit_units,
    a.note,
    po.actual_outcome
FROM model.value_bets vb
JOIN core.fixtures f ON f.fixture_id = vb.fixture_id
JOIN core.leagues l ON l.league_id = f.league_id
JOIN core.teams ht ON ht.team_id = f.home_team_id
JOIN core.teams at ON at.team_id = f.away_team_id
LEFT JOIN model.prediction_outcomes po ON po.fixture_id = vb.fixture_id
LEFT JOIN model.user_bet_annotations a
       ON a.fixture_id = vb.fixture_id
      AND a.market_code = vb.market_code
      AND a.selection_code = vb.selection_code
      AND a.bet_kind = 'DEAL'
LEFT JOIN position_rollup pr
       ON pr.fixture_id = vb.fixture_id
      AND pr.market_code = vb.market_code
      AND pr.selection_code = vb.selection_code
      AND pr.bet_kind = 'DEAL'
WHERE vb.status_code = 'ACTIVE'

UNION ALL

SELECT
    'PRONOSTIC' AS bet_kind,
    po.fixture_id,
    NULL::bigint AS outright_market_id,
    l.league_name,
    ht.team_name AS home_team_name,
    at.team_name AS away_team_name,
    f.kickoff_utc,
    '1X2' AS market_code,
    po.pronostic AS selection_code,
    NULL::text AS subject_label,
    CASE
        WHEN po.pronostic = 'HOME' THEN lp.home_win_probability
        WHEN po.pronostic = 'DRAW' THEN lp.draw_probability
        WHEN po.pronostic = 'AWAY' THEN lp.away_win_probability
        ELSE pr.model_probability
    END AS model_probability,
    CASE
        WHEN po.pronostic = 'HOME' AND lp.home_win_probability > 0 THEN ROUND((1 / lp.home_win_probability)::numeric, 4)
        WHEN po.pronostic = 'DRAW' AND lp.draw_probability > 0 THEN ROUND((1 / lp.draw_probability)::numeric, 4)
        WHEN po.pronostic = 'AWAY' AND lp.away_win_probability > 0 THEN ROUND((1 / lp.away_win_probability)::numeric, 4)
        ELSE pr.model_fair_odd
    END AS model_fair_odd,
    NULL::numeric AS market_odd,
    NULL::numeric AS edge_probability,
    CASE WHEN po.is_correct THEN 'WON' ELSE 'LOST' END AS result_code,
    NULL::numeric AS profit_units,
    po.settled_at,
    COALESCE(pr.taken, false) AS taken,
    COALESCE(pr.position_count, 0) AS position_count,
    pr.taken_odd,
    pr.stake_amount,
    pr.taken_at,
    CASE
        WHEN COALESCE(pr.taken, false) = false THEN NULL::numeric
        WHEN po.is_correct AND pr.taken_odd IS NOT NULL
            THEN ROUND((COALESCE(pr.stake_amount, 1) * (pr.taken_odd - 1))::numeric, 4)
        WHEN NOT po.is_correct
            THEN ROUND((-COALESCE(pr.stake_amount, 1))::numeric, 4)
        ELSE NULL::numeric
    END AS taken_profit_units,
    a.note,
    po.actual_outcome
FROM model.prediction_outcomes po
JOIN core.fixtures f ON f.fixture_id = po.fixture_id
JOIN core.leagues l ON l.league_id = f.league_id
JOIN core.teams ht ON ht.team_id = f.home_team_id
JOIN core.teams at ON at.team_id = f.away_team_id
LEFT JOIN latest_prediction lp ON lp.prediction_id = po.prediction_id
LEFT JOIN model.user_bet_annotations a
       ON a.fixture_id = po.fixture_id
      AND a.market_code = '1X2'
      AND a.selection_code = po.pronostic
      AND a.bet_kind = 'PRONOSTIC'
LEFT JOIN position_rollup pr
       ON pr.fixture_id = po.fixture_id
      AND pr.market_code = '1X2'
      AND pr.selection_code = po.pronostic
      AND pr.bet_kind = 'PRONOSTIC'

UNION ALL

SELECT
    'PRONOSTIC' AS bet_kind,
    pr.fixture_id,
    NULL::bigint AS outright_market_id,
    l.league_name,
    ht.team_name AS home_team_name,
    at.team_name AS away_team_name,
    f.kickoff_utc,
    pr.market_code,
    pr.selection_code,
    pr.selection_label AS subject_label,
    pr.model_probability,
    pr.model_fair_odd,
    NULL::numeric AS market_odd,
    NULL::numeric AS edge_probability,
    CASE
        WHEN po.home_reg IS NULL OR po.away_reg IS NULL THEN NULL::text
        WHEN pr.market_code = 'EXACT_SCORE'
            THEN CASE WHEN pr.selection_code = (po.home_reg::text || '-' || po.away_reg::text) THEN 'WON' ELSE 'LOST' END
        WHEN pr.selection_code = 'OVER'
            THEN CASE WHEN po.home_reg + po.away_reg >= 3 THEN 'WON' ELSE 'LOST' END
        WHEN pr.selection_code = 'UNDER'
            THEN CASE WHEN po.home_reg + po.away_reg <= 2 THEN 'WON' ELSE 'LOST' END
        WHEN pr.selection_code = 'BTTS_YES'
            THEN CASE WHEN po.home_reg > 0 AND po.away_reg > 0 THEN 'WON' ELSE 'LOST' END
        WHEN pr.selection_code = 'BTTS_NO'
            THEN CASE WHEN po.home_reg = 0 OR po.away_reg = 0 THEN 'WON' ELSE 'LOST' END
        WHEN pr.selection_code = 'HOME'
            THEN CASE WHEN po.home_reg > po.away_reg THEN 'WON' ELSE 'LOST' END
        WHEN pr.selection_code = 'DRAW'
            THEN CASE WHEN po.home_reg = po.away_reg THEN 'WON' ELSE 'LOST' END
        WHEN pr.selection_code = 'AWAY'
            THEN CASE WHEN po.away_reg > po.home_reg THEN 'WON' ELSE 'LOST' END
        ELSE NULL::text
    END AS result_code,
    NULL::numeric AS profit_units,
    po.settled_at,
    true AS taken,
    pr.position_count,
    pr.taken_odd,
    pr.stake_amount,
    pr.taken_at,
    CASE
        WHEN po.home_reg IS NULL OR po.away_reg IS NULL THEN NULL::numeric
        WHEN (
            (pr.market_code = 'EXACT_SCORE' AND pr.selection_code = (po.home_reg::text || '-' || po.away_reg::text))
            OR (pr.selection_code = 'OVER' AND po.home_reg + po.away_reg >= 3)
            OR (pr.selection_code = 'UNDER' AND po.home_reg + po.away_reg <= 2)
            OR (pr.selection_code = 'BTTS_YES' AND po.home_reg > 0 AND po.away_reg > 0)
            OR (pr.selection_code = 'BTTS_NO' AND (po.home_reg = 0 OR po.away_reg = 0))
            OR (pr.selection_code = 'HOME' AND po.home_reg > po.away_reg)
            OR (pr.selection_code = 'DRAW' AND po.home_reg = po.away_reg)
            OR (pr.selection_code = 'AWAY' AND po.away_reg > po.home_reg)
        ) THEN ROUND((pr.stake_amount * (pr.taken_odd - 1))::numeric, 4)
        ELSE ROUND((-pr.stake_amount)::numeric, 4)
    END AS taken_profit_units,
    a.note,
    po.actual_outcome
FROM position_rollup pr
JOIN core.fixtures f ON f.fixture_id = pr.fixture_id
JOIN core.leagues l ON l.league_id = f.league_id
JOIN core.teams ht ON ht.team_id = f.home_team_id
JOIN core.teams at ON at.team_id = f.away_team_id
LEFT JOIN model.prediction_outcomes po ON po.fixture_id = pr.fixture_id
LEFT JOIN model.user_bet_annotations a
       ON a.fixture_id = pr.fixture_id
      AND a.market_code = pr.market_code
      AND a.selection_code = pr.selection_code
      AND a.bet_kind = 'PRONOSTIC'
WHERE pr.bet_kind = 'PRONOSTIC'
  AND (
      po.fixture_id IS NULL
      OR pr.market_code <> '1X2'
      OR pr.selection_code IS DISTINCT FROM po.pronostic
  )

UNION ALL

SELECT
    'OUTRIGHT' AS bet_kind,
    NULL::bigint AS fixture_id,
    od.outright_market_id,
    l.league_name,
    NULL::text AS home_team_name,
    NULL::text AS away_team_name,
    m.deadline_utc AS kickoff_utc,
    m.market_code,
    s.subject_label AS selection_code,
    s.subject_label,
    od.model_probability,
    CASE
        WHEN od.model_probability > 0 THEN ROUND((1 / od.model_probability)::numeric, 4)
        ELSE NULL::numeric
    END AS model_fair_odd,
    od.market_odd,
    od.edge_probability,
    od.result_code,
    od.profit_units,
    od.settled_at,
    COALESCE(pr.taken, false) AS taken,
    COALESCE(pr.position_count, 0) AS position_count,
    pr.taken_odd,
    pr.stake_amount,
    pr.taken_at,
    CASE
        WHEN COALESCE(pr.taken, false) = false THEN NULL::numeric
        WHEN od.result_code = 'WON' AND pr.taken_odd IS NOT NULL
            THEN ROUND((COALESCE(pr.stake_amount, 1) * (pr.taken_odd - 1))::numeric, 4)
        WHEN od.result_code = 'LOST'
            THEN ROUND((-COALESCE(pr.stake_amount, 1))::numeric, 4)
        WHEN od.result_code = 'VOID'
            THEN 0::numeric
        ELSE NULL::numeric
    END AS taken_profit_units,
    a.note,
    NULL::text AS actual_outcome
FROM model.outright_deals od
JOIN core.outright_markets m ON m.outright_market_id = od.outright_market_id
JOIN core.outright_selections s ON s.outright_selection_id = od.outright_selection_id
LEFT JOIN core.leagues l ON l.league_id = m.league_id
LEFT JOIN model.user_bet_annotations a
       ON a.outright_market_id = od.outright_market_id
      AND a.selection_code = s.subject_label
      AND a.bet_kind = 'OUTRIGHT'
LEFT JOIN position_rollup pr
       ON pr.outright_market_id = od.outright_market_id
      AND pr.selection_code = s.subject_label
      AND pr.bet_kind = 'OUTRIGHT'
WHERE od.status_code = 'ACTIVE';

GRANT SELECT, INSERT, DELETE ON model.user_bet_positions TO spe_app_rw;
GRANT SELECT ON model.user_bet_positions TO spe_readonly;
GRANT USAGE, SELECT ON SEQUENCE model.user_bet_positions_position_id_seq TO spe_app_rw;
GRANT SELECT ON reporting.v_track_record TO spe_app_rw, spe_readonly;

COMMIT;
