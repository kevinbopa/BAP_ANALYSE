-- 0038 : authentification, roles, audit et bankroll multi-utilisateur.
--
-- Objectif produit :
-- - deux roles (CLIENT / ADMIN)
-- - sessions serveur via cookie
-- - journal d'audit exploitable
-- - bankroll personnelle historisee
-- - prises, notes et combines rattaches a un utilisateur
--
-- Les donnees existantes sont conservees et assignees a un proprietaire local
-- technique. Le vrai admin se cree ensuite via scripts/bootstrap_admin.py.

BEGIN;

CREATE SCHEMA IF NOT EXISTS app_auth;

CREATE TABLE IF NOT EXISTS app_auth.users (
    user_id bigserial PRIMARY KEY,
    email text NOT NULL,
    display_name text NOT NULL,
    password_hash text NOT NULL,
    role_code text NOT NULL DEFAULT 'CLIENT',
    status_code text NOT NULL DEFAULT 'ACTIVE',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz,
    deleted_at timestamptz,
    CHECK (position('@' in email) > 1),
    CHECK (role_code IN ('CLIENT', 'ADMIN')),
    CHECK (status_code IN ('ACTIVE', 'DISABLED', 'PENDING'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_app_users_email_lower
    ON app_auth.users (lower(email))
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS app_auth.sessions (
    session_id bigserial PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES app_auth.users(user_id) ON DELETE CASCADE,
    token_hash text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    user_agent text,
    ip_address text,
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_active
    ON app_auth.sessions (user_id, expires_at)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS app_auth.audit_log (
    audit_id bigserial PRIMARY KEY,
    actor_user_id bigint REFERENCES app_auth.users(user_id) ON DELETE SET NULL,
    target_user_id bigint REFERENCES app_auth.users(user_id) ON DELETE SET NULL,
    action_code text NOT NULL,
    entity_type text,
    entity_id text,
    route text,
    ip_address text,
    user_agent text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_actor_created
    ON app_auth.audit_log (actor_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_target_created
    ON app_auth.audit_log (target_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action_created
    ON app_auth.audit_log (action_code, created_at DESC);

INSERT INTO app_auth.users (email, display_name, password_hash, role_code, status_code)
VALUES ('local-owner@bp-edge.local', 'Local owner', 'DISABLED', 'ADMIN', 'DISABLED')
ON CONFLICT DO NOTHING;

ALTER TABLE model.user_bet_annotations
    ADD COLUMN IF NOT EXISTS user_id bigint REFERENCES app_auth.users(user_id) ON DELETE CASCADE;

UPDATE model.user_bet_annotations
SET user_id = (SELECT user_id FROM app_auth.users WHERE email = 'local-owner@bp-edge.local')
WHERE user_id IS NULL;

ALTER TABLE model.user_bet_annotations
    ALTER COLUMN user_id SET NOT NULL;

ALTER TABLE model.user_bet_annotations
    DROP CONSTRAINT IF EXISTS uq_user_bet_annotations_recommendation;

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_bet_annotations_user_reco
    ON model.user_bet_annotations (
        user_id,
        COALESCE(fixture_id, -1),
        COALESCE(outright_market_id, -1),
        market_code,
        selection_code,
        bet_kind
    );

ALTER TABLE model.user_bet_positions
    ADD COLUMN IF NOT EXISTS user_id bigint REFERENCES app_auth.users(user_id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz,
    ADD COLUMN IF NOT EXISTS deleted_by bigint REFERENCES app_auth.users(user_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS delete_reason text;

UPDATE model.user_bet_positions
SET user_id = (SELECT user_id FROM app_auth.users WHERE email = 'local-owner@bp-edge.local')
WHERE user_id IS NULL;

ALTER TABLE model.user_bet_positions
    ALTER COLUMN user_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_user_positions_user_taken
    ON model.user_bet_positions (user_id, taken_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_user_positions_user_reco
    ON model.user_bet_positions (
        user_id, bet_kind, fixture_id, outright_market_id, market_code, selection_code, line
    )
    WHERE deleted_at IS NULL;

ALTER TABLE model.parlay_tickets
    ADD COLUMN IF NOT EXISTS user_id bigint REFERENCES app_auth.users(user_id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz,
    ADD COLUMN IF NOT EXISTS deleted_by bigint REFERENCES app_auth.users(user_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS delete_reason text;

UPDATE model.parlay_tickets
SET user_id = (SELECT user_id FROM app_auth.users WHERE email = 'local-owner@bp-edge.local')
WHERE user_id IS NULL;

ALTER TABLE model.parlay_tickets
    ALTER COLUMN user_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_parlay_tickets_user_taken
    ON model.parlay_tickets (user_id, taken_at DESC)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS model.user_bankrolls (
    bankroll_id bigserial PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES app_auth.users(user_id) ON DELETE CASCADE,
    currency_code text NOT NULL DEFAULT 'CAD',
    current_amount numeric(14, 2) NOT NULL DEFAULT 0,
    status_code text NOT NULL DEFAULT 'ACTIVE',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (current_amount >= 0),
    CHECK (status_code IN ('ACTIVE', 'DISABLED'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_bankroll_active
    ON model.user_bankrolls (user_id)
    WHERE status_code = 'ACTIVE';

CREATE TABLE IF NOT EXISTS model.user_bankroll_events (
    event_id bigserial PRIMARY KEY,
    bankroll_id bigint NOT NULL REFERENCES model.user_bankrolls(bankroll_id) ON DELETE CASCADE,
    user_id bigint NOT NULL REFERENCES app_auth.users(user_id) ON DELETE CASCADE,
    actor_user_id bigint REFERENCES app_auth.users(user_id) ON DELETE SET NULL,
    event_type text NOT NULL,
    amount_delta numeric(14, 2) NOT NULL,
    resulting_amount numeric(14, 2) NOT NULL,
    reason text,
    position_id bigint REFERENCES model.user_bet_positions(position_id) ON DELETE SET NULL,
    parlay_id bigint REFERENCES model.parlay_tickets(parlay_id) ON DELETE SET NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (event_type IN (
        'BANKROLL_SET', 'MANUAL_ADJUSTMENT', 'STAKE_PLACED',
        'STAKE_VOIDED', 'BET_WON', 'BET_LOST', 'BET_VOID'
    )),
    CHECK (resulting_amount >= 0)
);

CREATE INDEX IF NOT EXISTS idx_bankroll_events_user_created
    ON model.user_bankroll_events (user_id, created_at DESC);

GRANT USAGE ON SCHEMA app_auth TO spe_app_rw, spe_readonly;
GRANT SELECT, INSERT, UPDATE, DELETE ON app_auth.users, app_auth.sessions, app_auth.audit_log TO spe_app_rw;
GRANT SELECT ON app_auth.users, app_auth.audit_log TO spe_readonly;
GRANT SELECT, INSERT, UPDATE, DELETE ON model.user_bankrolls, model.user_bankroll_events TO spe_app_rw;
GRANT SELECT ON model.user_bankrolls, model.user_bankroll_events TO spe_readonly;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app_auth TO spe_app_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA model TO spe_app_rw;

COMMIT;
