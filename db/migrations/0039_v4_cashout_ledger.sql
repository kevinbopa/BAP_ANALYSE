-- 0039 : cashout de position + evenement ledger CASHOUT.
--
-- Un client peut couper un pari en cours de match chez son bookmaker
-- (cashout). On enregistre le montant recupere et l'instant : la position
-- sort du reglement par resultat (son P&L = cashout - mise) et la bankroll
-- est creditee immediatement via un evenement CASHOUT au ledger.

BEGIN;

ALTER TABLE model.user_bet_positions
    ADD COLUMN IF NOT EXISTS cashout_amount numeric(14, 2),
    ADD COLUMN IF NOT EXISTS cashed_out_at timestamptz;

ALTER TABLE model.user_bet_positions
    DROP CONSTRAINT IF EXISTS user_bet_positions_cashout_chk;
ALTER TABLE model.user_bet_positions
    ADD CONSTRAINT user_bet_positions_cashout_chk
    CHECK (cashout_amount IS NULL OR cashout_amount >= 0);

ALTER TABLE model.user_bankroll_events
    DROP CONSTRAINT IF EXISTS user_bankroll_events_event_type_check;
ALTER TABLE model.user_bankroll_events
    ADD CONSTRAINT user_bankroll_events_event_type_check
    CHECK (event_type IN (
        'BANKROLL_SET', 'MANUAL_ADJUSTMENT', 'STAKE_PLACED',
        'STAKE_VOIDED', 'BET_WON', 'BET_LOST', 'BET_VOID', 'CASHOUT'
    ));

COMMIT;
