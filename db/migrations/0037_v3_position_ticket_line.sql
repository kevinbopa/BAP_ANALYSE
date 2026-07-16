-- 0037 : memorise la ligne de pari dans chaque ticket utilisateur.
--
-- Indispensable pour les handicaps : "Domicile handicap -1,5" et
-- "Domicile handicap -0,5" sont deux positions differentes, meme si elles
-- partagent fixture_id + market_code + selection_code.

BEGIN;

ALTER TABLE model.user_bet_positions
    ADD COLUMN IF NOT EXISTS line numeric(5, 2);

CREATE INDEX IF NOT EXISTS idx_user_positions_reco_line
    ON model.user_bet_positions (
        bet_kind, fixture_id, outright_market_id, market_code, selection_code, line
    );

COMMIT;
