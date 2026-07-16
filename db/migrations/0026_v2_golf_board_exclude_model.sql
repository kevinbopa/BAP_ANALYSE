-- 0026 : exclure le book synthetique DATAGOLF du "meilleur prix" du board.
--
-- DATAGOLF n'est pas un book jouable : c'est le fair-odd du modele (sans vig),
-- donc souvent la cote la plus haute. Il polluait la colonne best_odd de
-- v_golf_board (Scheffler affiche @ DATAGOLF au lieu d'un vrai book). On
-- l'exclut du calcul du meilleur prix, comme les exchanges. Le modele lui-meme
-- vient de model.golf_predictions, pas de cette colonne.

BEGIN;

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
    WHERE b.bookmaker_code <> 'DATAGOLF'
      AND b.bookmaker_code !~ '_EX_'
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

GRANT SELECT ON reporting.v_golf_board TO spe_app_rw, spe_ingest_rw, spe_readonly;

COMMIT;
