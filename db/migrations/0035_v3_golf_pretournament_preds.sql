-- 0035 : probas pre-tournoi DataGolf (les DEUX modeles) — source primaire V3.
--
-- Avant, la proba modele transitait par le fair-odd du pseudo-book DATAGOLF
-- dans core.golf_odds : un seul modele, seulement les joueurs cotes, non
-- normalise. Cette table stocke les probas brutes des DEUX modeles DataGolf
-- (baseline = skill seul ; fit = baseline_history_fit ou baseline_history
-- selon l'event) pour TOUT le field. Le moteur (spe_prediction/golf_engine.py)
-- les blende et les normalise -> model.golf_predictions. Meme decoupage que le
-- foot : couche donnees (core) -> moteur pur -> model.

BEGIN;

CREATE TABLE IF NOT EXISTS core.golf_pretournament_preds (
    golf_pretournament_pred_id bigserial PRIMARY KEY,
    golf_tournament_id bigint NOT NULL REFERENCES core.golf_tournaments(golf_tournament_id) ON DELETE CASCADE,
    golf_player_id bigint REFERENCES core.golf_players(golf_player_id) ON DELETE SET NULL,
    dg_id bigint NOT NULL,
    player_name text,
    market_code text NOT NULL,
    prob_baseline numeric(9, 6),
    prob_fit numeric(9, 6),
    sample_size integer,
    captured_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (golf_tournament_id, dg_id, market_code),
    CHECK (market_code IN ('TOURNAMENT_WINNER', 'TOP_5', 'TOP_10', 'TOP_20', 'MAKE_CUT'))
);

CREATE INDEX IF NOT EXISTS idx_golf_pretournament_preds_tournament
    ON core.golf_pretournament_preds (golf_tournament_id, market_code);

GRANT SELECT, INSERT, UPDATE, DELETE ON core.golf_pretournament_preds TO spe_ingest_rw;
GRANT SELECT ON core.golf_pretournament_preds TO spe_app_rw, spe_readonly;
GRANT USAGE, SELECT ON SEQUENCE core.golf_pretournament_preds_golf_pretournament_pred_id_seq TO spe_ingest_rw;

COMMIT;
