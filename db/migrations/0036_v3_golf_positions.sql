-- 0036 : prises de position sur les deals GOLF (argent reel).
--
-- user_bet_positions ne referencait que fixture/outright (football) : les
-- deals golf n'etaient pas prenables. On ajoute les references golf ; chaque
-- position enregistre TOUJOURS cote reelle, mise et date (colonnes NOT NULL
-- existantes) -> suivi rigoureux, vendable.

BEGIN;

ALTER TABLE model.user_bet_positions
    ADD COLUMN IF NOT EXISTS golf_deal_id bigint REFERENCES model.golf_deals(golf_deal_id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS golf_matchup_deal_id bigint REFERENCES model.golf_matchup_deals(golf_matchup_deal_id) ON DELETE CASCADE;

ALTER TABLE model.user_bet_positions DROP CONSTRAINT IF EXISTS user_bet_positions_bet_kind_check;
ALTER TABLE model.user_bet_positions ADD CONSTRAINT user_bet_positions_bet_kind_check
    CHECK (bet_kind = ANY (ARRAY['DEAL', 'PRONOSTIC', 'OUTRIGHT', 'GOLF']));

ALTER TABLE model.user_bet_positions DROP CONSTRAINT IF EXISTS user_bet_positions_check;
ALTER TABLE model.user_bet_positions ADD CONSTRAINT user_bet_positions_check
    CHECK (fixture_id IS NOT NULL OR outright_market_id IS NOT NULL
           OR golf_deal_id IS NOT NULL OR golf_matchup_deal_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_user_bet_positions_golf
    ON model.user_bet_positions (golf_deal_id) WHERE golf_deal_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_user_bet_positions_golf_matchup
    ON model.user_bet_positions (golf_matchup_deal_id) WHERE golf_matchup_deal_id IS NOT NULL;

COMMIT;
