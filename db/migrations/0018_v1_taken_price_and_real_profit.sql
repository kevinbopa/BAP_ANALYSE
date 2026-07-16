-- 0018 : cote/mise reelles sur les positions prises.
--
-- La page principale devient le seul endroit ou l'utilisateur declare une
-- prise de position live. On stocke la cote prise et la mise pour calculer
-- le profit reel dans le Back, au lieu d'utiliser seulement la cote modele.

BEGIN;

ALTER TABLE model.user_bet_annotations
    ADD COLUMN IF NOT EXISTS taken_odd numeric(10, 4),
    ADD COLUMN IF NOT EXISTS stake_amount numeric(12, 4),
    ADD COLUMN IF NOT EXISTS taken_at timestamptz;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_user_annotations_taken_odd'
    ) THEN
        ALTER TABLE model.user_bet_annotations
            ADD CONSTRAINT chk_user_annotations_taken_odd
            CHECK (taken_odd IS NULL OR taken_odd > 1);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_user_annotations_stake_amount'
    ) THEN
        ALTER TABLE model.user_bet_annotations
            ADD CONSTRAINT chk_user_annotations_stake_amount
            CHECK (stake_amount IS NULL OR stake_amount > 0);
    END IF;
END $$;

DROP VIEW IF EXISTS reporting.v_track_record;

CREATE VIEW reporting.v_track_record AS
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
    vb.market_odd,
    vb.edge_probability,
    vb.result_code,
    vb.profit_units,
    vb.settled_at,
    a.taken,
    a.taken_odd,
    a.stake_amount,
    a.taken_at,
    CASE
        WHEN COALESCE(a.taken, false) = false THEN NULL::numeric
        WHEN vb.result_code = 'WON' AND a.taken_odd IS NOT NULL
            THEN ROUND((COALESCE(a.stake_amount, 1) * (a.taken_odd - 1))::numeric, 4)
        WHEN vb.result_code = 'LOST'
            THEN ROUND((-COALESCE(a.stake_amount, 1))::numeric, 4)
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
    NULL::numeric AS model_probability,
    NULL::numeric AS market_odd,
    NULL::numeric AS edge_probability,
    CASE WHEN po.is_correct THEN 'WON' ELSE 'LOST' END AS result_code,
    NULL::numeric AS profit_units,
    po.settled_at,
    a.taken,
    a.taken_odd,
    a.stake_amount,
    a.taken_at,
    CASE
        WHEN COALESCE(a.taken, false) = false THEN NULL::numeric
        WHEN po.is_correct AND a.taken_odd IS NOT NULL
            THEN ROUND((COALESCE(a.stake_amount, 1) * (a.taken_odd - 1))::numeric, 4)
        WHEN NOT po.is_correct
            THEN ROUND((-COALESCE(a.stake_amount, 1))::numeric, 4)
        ELSE NULL::numeric
    END AS taken_profit_units,
    a.note,
    po.actual_outcome
FROM model.prediction_outcomes po
JOIN core.fixtures f ON f.fixture_id = po.fixture_id
JOIN core.leagues l ON l.league_id = f.league_id
JOIN core.teams ht ON ht.team_id = f.home_team_id
JOIN core.teams at ON at.team_id = f.away_team_id
LEFT JOIN model.user_bet_annotations a
       ON a.fixture_id = po.fixture_id
      AND a.market_code = '1X2'
      AND a.selection_code = po.pronostic
      AND a.bet_kind = 'PRONOSTIC'

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
    od.market_odd,
    od.edge_probability,
    od.result_code,
    od.profit_units,
    od.settled_at,
    a.taken,
    a.taken_odd,
    a.stake_amount,
    a.taken_at,
    CASE
        WHEN COALESCE(a.taken, false) = false THEN NULL::numeric
        WHEN od.result_code = 'WON' AND a.taken_odd IS NOT NULL
            THEN ROUND((COALESCE(a.stake_amount, 1) * (a.taken_odd - 1))::numeric, 4)
        WHEN od.result_code = 'LOST'
            THEN ROUND((-COALESCE(a.stake_amount, 1))::numeric, 4)
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
WHERE od.status_code = 'ACTIVE';

GRANT SELECT ON reporting.v_track_record TO spe_app_rw, spe_readonly;

COMMIT;
