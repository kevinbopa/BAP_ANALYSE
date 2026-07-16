-- 0034 : catalogue golf + scope de calcul.
--
-- Objectif produit :
--   * stocker tous les championnats DataGolf connus via le schedule,
--     pas seulement l'evenement actif du tour;
--   * distinguer "catalogue connu" vs "field/cotes synchronises";
--   * ajouter des index pour lancer predictions/deals par championnat,
--     tour ou fenetre de dates sans balayer toute la base.

BEGIN;

ALTER TABLE core.golf_tournaments
    ADD COLUMN IF NOT EXISTS catalog_status text NOT NULL DEFAULT 'DISCOVERED',
    ADD COLUMN IF NOT EXISTS last_catalog_sync_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_field_sync_at timestamptz;

ALTER TABLE core.golf_tournaments
    DROP CONSTRAINT IF EXISTS chk_golf_tournaments_catalog_status;

ALTER TABLE core.golf_tournaments
    ADD CONSTRAINT chk_golf_tournaments_catalog_status
    CHECK (catalog_status IN ('DISCOVERED', 'FIELD_SYNCED', 'ODDS_SYNCED', 'COMPLETED', 'UNSUPPORTED'));

CREATE INDEX IF NOT EXISTS idx_golf_tournaments_scope
    ON core.golf_tournaments (tour_code, date_start, golf_tournament_id);

CREATE INDEX IF NOT EXISTS idx_golf_tournaments_catalog_status
    ON core.golf_tournaments (catalog_status, date_start NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_golf_odds_scope_latest
    ON core.golf_odds (golf_tournament_id, market_code, bookmaker_id, captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_golf_matchup_odds_scope_latest
    ON core.golf_matchup_odds (golf_tournament_id, market_code, bookmaker_id, captured_at DESC);

COMMIT;
