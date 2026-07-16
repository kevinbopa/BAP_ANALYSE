-- 0031 : couche de donnees SG (strokes-gained) DataGolf par joueur.
--
-- On NE re-modelise PAS (le baseline_history_fit de DataGolf combine deja SG +
-- course-fit mieux qu'on ne le ferait). Cette table sert la TRANSPARENCE (why),
-- le controle qualite, et de futures features. Une ligne par joueur (snapshot
-- courant), cle dg_id.

BEGIN;

CREATE TABLE IF NOT EXISTS core.golf_skill_ratings (
    golf_skill_rating_id bigserial PRIMARY KEY,
    dg_id bigint NOT NULL UNIQUE,
    golf_player_id bigint REFERENCES core.golf_players(golf_player_id) ON DELETE SET NULL,
    player_name text,
    sg_total numeric(6, 3),
    sg_ott numeric(6, 3),      -- off the tee
    sg_app numeric(6, 3),      -- approche
    sg_arg numeric(6, 3),      -- autour du green
    sg_putt numeric(6, 3),     -- putting
    driving_acc numeric(6, 3),
    driving_dist numeric(7, 3),
    dg_skill_estimate numeric(6, 3),
    datagolf_rank integer,
    owgr_rank integer,
    updated_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON core.golf_skill_ratings TO spe_ingest_rw, spe_app_rw;
GRANT SELECT ON core.golf_skill_ratings TO spe_readonly;
GRANT USAGE, SELECT ON SEQUENCE core.golf_skill_ratings_golf_skill_rating_id_seq TO spe_ingest_rw, spe_app_rw;

COMMIT;
