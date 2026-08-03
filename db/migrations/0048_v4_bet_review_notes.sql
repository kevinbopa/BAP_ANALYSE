-- 0048 : notes personnelles et status de revision sur les paris pris.
--
-- Permet a l'utilisateur de repasser sur ses paris (par plage de dates
-- via /back) pour :
--   * ajouter une note personnelle (contexte, motivation, apprentissage)
--   * corriger une valeur (cote, mise, cashout) si necessaire
--   * marquer le pari comme « revise » = deja passe en revue
--
-- Les notes sortent dans le rapport annuel Excel. Le flag revise permet de
-- filtrer les paris qui n'ont pas encore ete audites.

BEGIN;

ALTER TABLE model.user_bet_positions
    ADD COLUMN IF NOT EXISTS user_note TEXT,
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reviewed_by INTEGER;

COMMENT ON COLUMN model.user_bet_positions.user_note IS
    'Note personnelle libre (contexte de la prise, apprentissage). Editable via /back.';
COMMENT ON COLUMN model.user_bet_positions.reviewed_at IS
    'Date de derniere revision (quand l''utilisateur a ferme la modale d''edition).';

-- Index pour filtrer rapidement les paris non revises dans /back.
CREATE INDEX IF NOT EXISTS ix_user_bet_positions_review_pending
    ON model.user_bet_positions (user_id, taken_at)
    WHERE reviewed_at IS NULL AND deleted_at IS NULL;

COMMIT;
