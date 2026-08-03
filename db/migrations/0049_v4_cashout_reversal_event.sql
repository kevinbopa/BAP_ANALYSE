-- 0049 : ajoute le type d'evenement CASHOUT_REVERSAL au ledger.
--
-- Cas d'usage : l'utilisateur declare un cashout sur bet365 APRES que
-- l'app ait deja regle le pari en WON/LOST/VOID. Le cashout bet365 prime
-- (source de verite metier), on doit donc annuler le reglement precedent
-- pour eviter le double-comptage bankroll.
--
-- Le CASHOUT_REVERSAL porte le delta oppose du reglement annule, garde le
-- lien position_id + audit metadata. Toujours suivi immediatement d'un
-- evenement CASHOUT normal dans la meme transaction.

BEGIN;

ALTER TABLE model.user_bankroll_events
    DROP CONSTRAINT IF EXISTS user_bankroll_events_event_type_check;

ALTER TABLE model.user_bankroll_events
    ADD CONSTRAINT user_bankroll_events_event_type_check
    CHECK (event_type = ANY (ARRAY[
        'BANKROLL_SET',
        'MANUAL_ADJUSTMENT',
        'STAKE_PLACED',
        'STAKE_VOIDED',
        'BET_WON',
        'BET_LOST',
        'BET_VOID',
        'CASHOUT',
        'CASHOUT_REVERSAL'
    ]));

COMMIT;
