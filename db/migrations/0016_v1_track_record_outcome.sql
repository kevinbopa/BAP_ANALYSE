-- 0016 : ajoute le RESULTAT REEL (1X2 reglementaire) a v_track_record.
--
-- Permet le filtre "perdu / match nul" : un pari HOME/AWAY perdu PARCE QUE
-- le match a fini sur un nul — exactement les cas ou le flag "nul tres
-- possible" du pronostic aurait pu prevenir.
--
-- model.prediction_outcomes.actual_outcome porte deja le 1X2 des 90 min
-- reglementaires pour chaque fixture reglee : on le joint aux deals.

BEGIN;

CREATE OR REPLACE VIEW reporting.v_track_record AS
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
