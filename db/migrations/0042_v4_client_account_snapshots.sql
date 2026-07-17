-- 0042 : snapshot client unifie (compte, bankroll, exposition, sessions).
--
-- Objectif :
-- - eviter les agregations ad hoc dans l'application ;
-- - exposer un etat client coherent pour l'admin et l'espace bankroll ;
-- - compter uniquement l'exposition encore ouverte (positions non reglees,
--   non cashout, non supprimees + combines en attente).

BEGIN;

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_last_seen
    ON app_auth.sessions (user_id, last_seen_at DESC)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_bankroll_events_position_event
    ON model.user_bankroll_events (position_id, event_type)
    WHERE position_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_bankroll_events_parlay_event
    ON model.user_bankroll_events (parlay_id, event_type)
    WHERE parlay_id IS NOT NULL;

CREATE OR REPLACE VIEW reporting.v_user_account_snapshot AS
WITH active_bankrolls AS (
    SELECT
        b.bankroll_id,
        b.user_id,
        b.currency_code,
        b.current_amount,
        b.updated_at
    FROM model.user_bankrolls b
    WHERE b.status_code = 'ACTIVE'
),
position_activity AS (
    SELECT
        p.user_id,
        p.position_id,
        p.bet_kind,
        p.stake_amount,
        p.taken_at,
        p.cashed_out_at,
        NOT EXISTS (
            SELECT 1
            FROM model.user_bankroll_events e
            WHERE e.position_id = p.position_id
              AND e.event_type IN ('BET_WON', 'BET_LOST', 'BET_VOID', 'CASHOUT')
        ) AS is_unsettled
    FROM model.user_bet_positions p
    WHERE p.deleted_at IS NULL
),
position_rollup AS (
    SELECT
        pa.user_id,
        COUNT(*)::integer AS total_positions,
        ROUND(COALESCE(SUM(pa.stake_amount), 0)::numeric, 2) AS total_stake_amount,
        MAX(pa.taken_at) AS last_position_at,
        COUNT(*) FILTER (
            WHERE pa.cashed_out_at IS NULL
              AND pa.is_unsettled
        )::integer AS open_position_count,
        ROUND(
            COALESCE(
                SUM(pa.stake_amount) FILTER (
                    WHERE pa.cashed_out_at IS NULL
                      AND pa.is_unsettled
                ),
                0
            )::numeric,
            2
        ) AS open_position_stake,
        ROUND(
            COALESCE(
                SUM(pa.stake_amount) FILTER (
                    WHERE pa.bet_kind = 'GOLF'
                      AND pa.cashed_out_at IS NULL
                      AND pa.is_unsettled
                ),
                0
            )::numeric,
            2
        ) AS open_golf_stake,
        ROUND(
            COALESCE(
                SUM(pa.stake_amount) FILTER (
                    WHERE pa.bet_kind <> 'GOLF'
                      AND pa.cashed_out_at IS NULL
                      AND pa.is_unsettled
                ),
                0
            )::numeric,
            2
        ) AS open_football_stake
    FROM position_activity pa
    GROUP BY pa.user_id
),
parlay_activity AS (
    SELECT
        pt.user_id,
        pt.parlay_id,
        pt.stake_amount,
        pt.taken_at,
        pt.result_code,
        NOT EXISTS (
            SELECT 1
            FROM model.user_bankroll_events e
            WHERE e.parlay_id = pt.parlay_id
              AND e.event_type IN ('BET_WON', 'BET_LOST', 'BET_VOID', 'CASHOUT')
        ) AS is_unsettled
    FROM model.parlay_tickets pt
    WHERE pt.deleted_at IS NULL
),
parlay_rollup AS (
    SELECT
        pa.user_id,
        COUNT(*) FILTER (
            WHERE pa.result_code IS NULL
              AND pa.is_unsettled
        )::integer AS open_parlay_count,
        ROUND(
            COALESCE(
                SUM(pa.stake_amount) FILTER (
                    WHERE pa.result_code IS NULL
                      AND pa.is_unsettled
                ),
                0
            )::numeric,
            2
        ) AS open_parlay_stake,
        MAX(pa.taken_at) AS last_parlay_at
    FROM parlay_activity pa
    GROUP BY pa.user_id
),
session_rollup AS (
    SELECT
        s.user_id,
        COUNT(*) FILTER (
            WHERE s.revoked_at IS NULL
              AND s.expires_at > now()
        )::integer AS active_session_count,
        MAX(s.last_seen_at) FILTER (
            WHERE s.revoked_at IS NULL
              AND s.expires_at > now()
        ) AS last_seen_at,
        MAX(s.expires_at) FILTER (
            WHERE s.revoked_at IS NULL
              AND s.expires_at > now()
        ) AS last_session_expires_at
    FROM app_auth.sessions s
    GROUP BY s.user_id
)
SELECT
    u.user_id,
    u.email,
    u.display_name,
    u.role_code,
    u.status_code,
    u.created_at,
    u.updated_at,
    u.last_login_at,
    ab.bankroll_id,
    COALESCE(ab.currency_code, 'CAD') AS currency_code,
    COALESCE(ab.current_amount, 0)::numeric(14, 2) AS bankroll_amount,
    ab.updated_at AS bankroll_updated_at,
    COALESCE(pr.total_positions, 0) AS total_positions,
    COALESCE(pr.total_stake_amount, 0)::numeric(14, 2) AS total_stake_amount,
    pr.last_position_at,
    COALESCE(pr.open_position_count, 0) AS open_position_count,
    COALESCE(pr.open_position_stake, 0)::numeric(14, 2) AS open_position_stake,
    COALESCE(pr.open_golf_stake, 0)::numeric(14, 2) AS open_golf_stake,
    COALESCE(pr.open_football_stake, 0)::numeric(14, 2) AS open_football_stake,
    COALESCE(py.open_parlay_count, 0) AS open_parlay_count,
    COALESCE(py.open_parlay_stake, 0)::numeric(14, 2) AS open_parlay_stake,
    py.last_parlay_at,
    ROUND(
        (
            COALESCE(pr.open_position_stake, 0)
            + COALESCE(py.open_parlay_stake, 0)
        )::numeric,
        2
    ) AS open_total_stake,
    COALESCE(sr.active_session_count, 0) AS active_session_count,
    sr.last_seen_at,
    sr.last_session_expires_at
FROM app_auth.users u
LEFT JOIN active_bankrolls ab ON ab.user_id = u.user_id
LEFT JOIN position_rollup pr ON pr.user_id = u.user_id
LEFT JOIN parlay_rollup py ON py.user_id = u.user_id
LEFT JOIN session_rollup sr ON sr.user_id = u.user_id
WHERE u.deleted_at IS NULL;

GRANT SELECT ON reporting.v_user_account_snapshot TO spe_app_rw, spe_readonly;

COMMIT;
