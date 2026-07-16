-- 0015 : espace "back" — verification des performances.
--
-- Deux besoins :
--   1. Regler les PRONOSTICS (pas seulement les deals) : le pronostic 1X2
--      du forecaster est-il tombe juste sur les 90 minutes reglementaires ?
--   2. Annotations utilisateur : "j'ai pris cette position" (case a cocher)
--      + note personnelle libre, par recommandation.

BEGIN;

-- 1. Resultat des pronostics 1X2 (une ligne par fixture, le dernier pronostic).
CREATE TABLE IF NOT EXISTS model.prediction_outcomes (
    fixture_id bigint PRIMARY KEY REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    prediction_id bigint NOT NULL REFERENCES model.predictions(prediction_id) ON DELETE CASCADE,
    pronostic text NOT NULL,
    actual_outcome text NOT NULL,
    is_correct boolean NOT NULL,
    surety_score numeric(8, 6),
    home_reg integer,
    away_reg integer,
    settled_at timestamptz NOT NULL DEFAULT now(),
    CHECK (pronostic IN ('HOME', 'DRAW', 'AWAY')),
    CHECK (actual_outcome IN ('HOME', 'DRAW', 'AWAY'))
);

CREATE INDEX IF NOT EXISTS idx_prediction_outcomes_settled
    ON model.prediction_outcomes (settled_at DESC);

-- 2. Annotations utilisateur. Une reco = (type, fixture ou marche outright,
--    marche, selection). taken = case cochee ; note = commentaire perso.
CREATE TABLE IF NOT EXISTS model.user_bet_annotations (
    annotation_id bigserial PRIMARY KEY,
    bet_kind text NOT NULL,
    fixture_id bigint REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    outright_market_id bigint REFERENCES core.outright_markets(outright_market_id) ON DELETE CASCADE,
    market_code text NOT NULL,
    selection_code text NOT NULL,
    taken boolean NOT NULL DEFAULT false,
    note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (fixture_id, outright_market_id, market_code, selection_code),
    CHECK (bet_kind IN ('DEAL', 'PRONOSTIC', 'OUTRIGHT')),
    CHECK (fixture_id IS NOT NULL OR outright_market_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_user_annotations_taken
    ON model.user_bet_annotations (taken) WHERE taken = true;

-- 3. Vue unifiee du track record : deals + pronostics regles, avec les
--    annotations utilisateur jointes. Alimente la page "back" et le graphe.
CREATE OR REPLACE VIEW reporting.v_track_record AS
-- Deals (value bets regles ou en attente)
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
    a.note
FROM model.value_bets vb
JOIN core.fixtures f ON f.fixture_id = vb.fixture_id
JOIN core.leagues l ON l.league_id = f.league_id
JOIN core.teams ht ON ht.team_id = f.home_team_id
JOIN core.teams at ON at.team_id = f.away_team_id
LEFT JOIN model.user_bet_annotations a
       ON a.fixture_id = vb.fixture_id
      AND a.market_code = vb.market_code
      AND a.selection_code = vb.selection_code
      AND a.bet_kind = 'DEAL'
WHERE vb.status_code = 'ACTIVE'

UNION ALL

-- Pronostics (regles via model.prediction_outcomes)
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
    a.note
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

-- Deals long terme (outright)
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
    a.note
FROM model.outright_deals od
JOIN core.outright_markets m ON m.outright_market_id = od.outright_market_id
JOIN core.outright_selections s ON s.outright_selection_id = od.outright_selection_id
LEFT JOIN core.leagues l ON l.league_id = m.league_id
LEFT JOIN model.user_bet_annotations a
       ON a.outright_market_id = od.outright_market_id
      AND a.selection_code = s.subject_label
      AND a.bet_kind = 'OUTRIGHT'
WHERE od.status_code = 'ACTIVE';

GRANT SELECT, INSERT, UPDATE, DELETE ON model.user_bet_annotations TO spe_app_rw;
GRANT SELECT, INSERT, UPDATE ON model.prediction_outcomes TO spe_app_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA model TO spe_app_rw;
GRANT SELECT ON reporting.v_track_record TO spe_app_rw, spe_readonly;

COMMIT;
