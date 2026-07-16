-- 0032 : resultats golf (positions finales) pour regler les deals.
--
-- Source : DataGolf in-play (current_pos + make_cut). L'ingestion ecrit ici
-- (core, role spe_ingest_rw) ; le reglement lit ici (prediction, spe_app_rw)
-- et solde model.golf_deals / model.golf_matchup_deals.

BEGIN;

CREATE TABLE IF NOT EXISTS core.golf_results (
    golf_result_id bigserial PRIMARY KEY,
    golf_tournament_id bigint NOT NULL REFERENCES core.golf_tournaments(golf_tournament_id) ON DELETE CASCADE,
    golf_player_id bigint REFERENCES core.golf_players(golf_player_id) ON DELETE SET NULL,
    dg_id bigint NOT NULL,
    position_text text,
    position_rank integer,
    made_cut boolean,
    current_round integer,
    total_score integer,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (golf_tournament_id, dg_id)
);

CREATE INDEX IF NOT EXISTS idx_golf_results_tournament
    ON core.golf_results (golf_tournament_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON core.golf_results TO spe_ingest_rw;
GRANT SELECT ON core.golf_results TO spe_app_rw, spe_readonly;
GRANT USAGE, SELECT ON SEQUENCE core.golf_results_golf_result_id_seq TO spe_ingest_rw;

COMMIT;
