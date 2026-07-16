-- 0040 : realigne la contrainte market/selection de value_bets sur le CODE.
--
-- Symptome : le pipeline plantait en CheckViolation sur les deals double
-- chance (le moteur emet DC_1X/DC_12/DC_X2 partout - repository, settlement,
-- ingesteur, dashboard - mais la base portait une vieille definition
-- HOME_DRAW/... issue de 0013, revenue lors d'un replay de migrations).
-- On restaure la definition de 0033 (source de verite = le code).

BEGIN;

ALTER TABLE model.value_bets DROP CONSTRAINT IF EXISTS value_bets_market_code_chk;
ALTER TABLE model.value_bets ADD CONSTRAINT value_bets_market_code_chk
    CHECK (market_code = ANY (ARRAY[
        '1X2', 'OU15', 'OU25', 'OU35', 'BTTS', 'DOUBLE_CHANCE', 'DNB', 'HANDICAP'
    ]));

ALTER TABLE model.value_bets DROP CONSTRAINT IF EXISTS value_bets_market_selection_chk;
ALTER TABLE model.value_bets ADD CONSTRAINT value_bets_market_selection_chk
    CHECK (
        (market_code = '1X2' AND selection_code = ANY (ARRAY['HOME', 'DRAW', 'AWAY']))
     OR (market_code IN ('OU15', 'OU25', 'OU35') AND selection_code = ANY (ARRAY['OVER', 'UNDER']))
     OR (market_code = 'BTTS' AND selection_code = ANY (ARRAY['BTTS_YES', 'BTTS_NO']))
     OR (market_code = 'DOUBLE_CHANCE' AND selection_code = ANY (ARRAY['DC_1X', 'DC_12', 'DC_X2']))
     OR (market_code = 'DNB' AND selection_code = ANY (ARRAY['HOME', 'AWAY']))
     OR (market_code = 'HANDICAP' AND selection_code = ANY (ARRAY['HOME', 'AWAY']))
    );

-- La contrainte plate selection_code_chk (heritee de 0013) est redondante
-- avec market_selection_chk et refuse DC_1X/12/X2 : on la supprime, la
-- combinaison marche+selection reste entierement verrouillee ci-dessus.
ALTER TABLE model.value_bets DROP CONSTRAINT IF EXISTS value_bets_selection_code_chk;

COMMIT;
