-- 0033 : marche HANDICAP (asiatique/europeen) dans model.value_bets.
--
-- Le handicap depend de la LIGNE (home -1, +1.25...) -> on ajoute une colonne
-- `line`. La proba vient de la distribution de marge (matrice Dixon-Coles, comme
-- O/U). Reglement asiatique (push ligne entiere, demi-gain/perte quarts) gere
-- cote moteur ; result_code reste WON/LOST/VOID (le profit_units porte le demi).

BEGIN;

ALTER TABLE model.value_bets ADD COLUMN IF NOT EXISTS line numeric(5, 2);

ALTER TABLE model.value_bets DROP CONSTRAINT IF EXISTS value_bets_market_code_chk;
ALTER TABLE model.value_bets DROP CONSTRAINT IF EXISTS value_bets_market_selection_chk;

ALTER TABLE model.value_bets ADD CONSTRAINT value_bets_market_code_chk
    CHECK (market_code = ANY (ARRAY[
        '1X2', 'OU15', 'OU25', 'OU35', 'BTTS', 'DOUBLE_CHANCE', 'DNB', 'HANDICAP'
    ]));

ALTER TABLE model.value_bets ADD CONSTRAINT value_bets_market_selection_chk
    CHECK (
        (market_code = '1X2' AND selection_code = ANY (ARRAY['HOME', 'DRAW', 'AWAY']))
     OR (market_code IN ('OU15', 'OU25', 'OU35') AND selection_code = ANY (ARRAY['OVER', 'UNDER']))
     OR (market_code = 'BTTS' AND selection_code = ANY (ARRAY['BTTS_YES', 'BTTS_NO']))
     OR (market_code = 'DOUBLE_CHANCE' AND selection_code = ANY (ARRAY['DC_1X', 'DC_12', 'DC_X2']))
     OR (market_code = 'DNB' AND selection_code = ANY (ARRAY['HOME', 'AWAY']))
     OR (market_code = 'HANDICAP' AND selection_code = ANY (ARRAY['HOME', 'AWAY']))
    );

COMMIT;
