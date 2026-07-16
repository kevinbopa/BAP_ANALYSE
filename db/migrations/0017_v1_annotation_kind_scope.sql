-- 0017 : annotations utilisateur separees par type de recommandation.
--
-- Une prise de position sur un PRONOSTIC et une prise de position sur un DEAL
-- du meme match ne doivent pas se confondre. Le code applicatif utilise
-- bet_kind dans la cle ; la contrainte SQL doit porter la meme semantique.

BEGIN;

ALTER TABLE model.user_bet_annotations
    DROP CONSTRAINT IF EXISTS user_bet_annotations_fixture_id_outright_market_id_market_code_selection_key;

ALTER TABLE model.user_bet_annotations
    DROP CONSTRAINT IF EXISTS uq_user_bet_annotations_recommendation;

ALTER TABLE model.user_bet_annotations
    ADD CONSTRAINT uq_user_bet_annotations_recommendation
    UNIQUE NULLS NOT DISTINCT (
        bet_kind,
        fixture_id,
        outright_market_id,
        market_code,
        selection_code
    );

COMMIT;
