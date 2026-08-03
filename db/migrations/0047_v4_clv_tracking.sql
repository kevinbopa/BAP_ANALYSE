-- 0047 : CLV tracking sur les prises (foot + golf).
--
-- CLV = Closing Line Value = ecart entre la cote au moment de la prise et
-- la derniere cote observee avant le coup d'envoi. C'est LE thermometre
-- « suis-je sharp » : sur 500 paris, le CLV moyen converge 8-10x plus vite
-- que le P&L brut vers la verite. Sans lui, on ne sait pas si un ROI positif
-- est du au modele ou a la chance.
--
-- Le calcul se fait ON DEMAND depuis core.fixture_odds_* / core.golf_odds :
-- on stocke juste le RESULTAT (cote closing, CLV%) pour ne pas re-scanner
-- l'historique a chaque affichage de /back.

BEGIN;

ALTER TABLE model.user_bet_positions
    ADD COLUMN IF NOT EXISTS closing_line_odd NUMERIC(8, 3),
    ADD COLUMN IF NOT EXISTS closing_line_at  TIMESTAMPTZ,
    -- CLV en % : (taken_odd / closing_odd) - 1, marges non retirees pour la
    -- v1 (l'ecart de marge entre livraisons du meme book est marginal).
    -- Positif = tu as pris a meilleur prix que le closing = SHARP.
    ADD COLUMN IF NOT EXISTS clv_pct          NUMERIC(6, 2);

COMMENT ON COLUMN model.user_bet_positions.clv_pct IS
    'Closing Line Value en %. >0 = ta cote de prise etait meilleure que la cote closing du marche = signal sharp.';

-- Index pour requetes agregees par marche/sport dans /back.
CREATE INDEX IF NOT EXISTS ix_user_bet_positions_clv
    ON model.user_bet_positions (bet_kind, market_code) WHERE clv_pct IS NOT NULL;

COMMIT;
