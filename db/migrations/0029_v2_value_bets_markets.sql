-- 0029 : ouvrir model.value_bets aux nouveaux marches jouables.
--
-- O/U 1.5 (OU15), O/U 3.5 (OU35), double chance (DOUBLE_CHANCE), draw-no-bet
-- (DNB). Le handicap viendra plus tard (colonne ligne + reglement asiatique).
-- Les codes O/U encodent la ligne dans le market_code (OU15/OU25/OU35), donc
-- pas besoin d'une colonne ligne pour ces marches.

BEGIN;

ALTER TABLE model.value_bets DROP CONSTRAINT IF EXISTS value_bets_market_code_chk;
ALTER TABLE model.value_bets DROP CONSTRAINT IF EXISTS value_bets_selection_code_chk;
ALTER TABLE model.value_bets DROP CONSTRAINT IF EXISTS value_bets_market_selection_chk;

ALTER TABLE model.value_bets ADD CONSTRAINT value_bets_market_code_chk
    CHECK (market_code = ANY (ARRAY[
        '1X2', 'OU15', 'OU25', 'OU35', 'BTTS', 'DOUBLE_CHANCE', 'DNB'
    ]));

ALTER TABLE model.value_bets ADD CONSTRAINT value_bets_selection_code_chk
    CHECK (selection_code = ANY (ARRAY[
        'HOME', 'DRAW', 'AWAY', 'OVER', 'UNDER', 'BTTS_YES', 'BTTS_NO',
        'DC_1X', 'DC_12', 'DC_X2'
    ]));

ALTER TABLE model.value_bets ADD CONSTRAINT value_bets_market_selection_chk
    CHECK (
        (market_code = '1X2' AND selection_code = ANY (ARRAY['HOME', 'DRAW', 'AWAY']))
     OR (market_code IN ('OU15', 'OU25', 'OU35') AND selection_code = ANY (ARRAY['OVER', 'UNDER']))
     OR (market_code = 'BTTS' AND selection_code = ANY (ARRAY['BTTS_YES', 'BTTS_NO']))
     OR (market_code = 'DOUBLE_CHANCE' AND selection_code = ANY (ARRAY['DC_1X', 'DC_12', 'DC_X2']))
     OR (market_code = 'DNB' AND selection_code = ANY (ARRAY['HOME', 'AWAY']))
    );

COMMIT;
