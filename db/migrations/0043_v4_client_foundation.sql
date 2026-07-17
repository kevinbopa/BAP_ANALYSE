-- 0043 : fondation client metier (profil, preferences, limites, tags, notes).

BEGIN;

CREATE TABLE IF NOT EXISTS app_auth.user_profiles (
    user_id bigint PRIMARY KEY REFERENCES app_auth.users(user_id) ON DELETE CASCADE,
    company_name text,
    phone text,
    country_code text,
    timezone_name text NOT NULL DEFAULT 'America/Toronto',
    segment_code text NOT NULL DEFAULT 'STANDARD',
    onboarding_status text NOT NULL DEFAULT 'ACTIVE',
    source_channel text,
    external_ref text,
    profile_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (segment_code IN ('STANDARD', 'VIP', 'PARTNER', 'INTERNAL')),
    CHECK (onboarding_status IN ('LEAD', 'ACTIVE', 'PAUSED', 'CHURNED'))
);

CREATE TABLE IF NOT EXISTS app_auth.user_preferences (
    user_id bigint PRIMARY KEY REFERENCES app_auth.users(user_id) ON DELETE CASCADE,
    preferred_sport text NOT NULL DEFAULT 'football',
    preferred_language text NOT NULL DEFAULT 'fr',
    preferred_currency text NOT NULL DEFAULT 'CAD',
    odds_format text NOT NULL DEFAULT 'DECIMAL',
    timezone_name text NOT NULL DEFAULT 'America/Toronto',
    alert_opt_in boolean NOT NULL DEFAULT false,
    marketing_opt_in boolean NOT NULL DEFAULT false,
    automation_opt_in boolean NOT NULL DEFAULT true,
    preference_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (odds_format IN ('DECIMAL', 'AMERICAN', 'FRACTIONAL'))
);

CREATE TABLE IF NOT EXISTS app_auth.user_limits (
    user_id bigint PRIMARY KEY REFERENCES app_auth.users(user_id) ON DELETE CASCADE,
    max_single_bet numeric(14, 2),
    max_daily_stake numeric(14, 2),
    max_open_exposure numeric(14, 2),
    loss_limit_daily numeric(14, 2),
    loss_limit_weekly numeric(14, 2),
    requires_manual_review boolean NOT NULL DEFAULT false,
    limits_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (max_single_bet IS NULL OR max_single_bet >= 0),
    CHECK (max_daily_stake IS NULL OR max_daily_stake >= 0),
    CHECK (max_open_exposure IS NULL OR max_open_exposure >= 0),
    CHECK (loss_limit_daily IS NULL OR loss_limit_daily >= 0),
    CHECK (loss_limit_weekly IS NULL OR loss_limit_weekly >= 0)
);

CREATE TABLE IF NOT EXISTS app_auth.client_tags (
    tag_id bigserial PRIMARY KEY,
    tag_code text NOT NULL UNIQUE,
    display_label text NOT NULL,
    color_token text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (btrim(tag_code) <> ''),
    CHECK (btrim(display_label) <> '')
);

CREATE TABLE IF NOT EXISTS app_auth.user_tag_links (
    user_id bigint NOT NULL REFERENCES app_auth.users(user_id) ON DELETE CASCADE,
    tag_id bigint NOT NULL REFERENCES app_auth.client_tags(tag_id) ON DELETE CASCADE,
    applied_by_user_id bigint REFERENCES app_auth.users(user_id) ON DELETE SET NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, tag_id)
);

CREATE TABLE IF NOT EXISTS app_auth.user_notes (
    note_id bigserial PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES app_auth.users(user_id) ON DELETE CASCADE,
    author_user_id bigint REFERENCES app_auth.users(user_id) ON DELETE SET NULL,
    note_kind text NOT NULL DEFAULT 'ADMIN',
    note_body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (note_kind IN ('ADMIN', 'SUPPORT', 'RISK', 'CRM')),
    CHECK (btrim(note_body) <> '')
);

CREATE INDEX IF NOT EXISTS idx_user_profiles_segment_status
    ON app_auth.user_profiles (segment_code, onboarding_status);

CREATE INDEX IF NOT EXISTS idx_user_notes_user_created
    ON app_auth.user_notes (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_user_tag_links_tag
    ON app_auth.user_tag_links (tag_id, applied_at DESC);

INSERT INTO app_auth.user_profiles (
    user_id,
    timezone_name,
    segment_code,
    onboarding_status,
    source_channel
)
SELECT
    u.user_id,
    'America/Toronto',
    CASE WHEN u.role_code = 'ADMIN' THEN 'INTERNAL' ELSE 'STANDARD' END,
    CASE WHEN u.status_code = 'PENDING' THEN 'LEAD' ELSE 'ACTIVE' END,
    'APP'
FROM app_auth.users u
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO app_auth.user_preferences (
    user_id,
    preferred_sport,
    preferred_language,
    preferred_currency,
    odds_format,
    timezone_name
)
SELECT
    u.user_id,
    'football',
    'fr',
    'CAD',
    'DECIMAL',
    'America/Toronto'
FROM app_auth.users u
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO app_auth.user_limits (
    user_id,
    max_single_bet,
    max_daily_stake,
    max_open_exposure,
    loss_limit_daily,
    loss_limit_weekly,
    requires_manual_review
)
SELECT
    u.user_id,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    false
FROM app_auth.users u
ON CONFLICT (user_id) DO NOTHING;

CREATE OR REPLACE VIEW reporting.v_user_admin_profile AS
WITH tag_rollup AS (
    SELECT
        utl.user_id,
        COUNT(*)::integer AS tag_count,
        string_agg(ct.display_label, ', ' ORDER BY ct.display_label) AS tags_csv
    FROM app_auth.user_tag_links utl
    JOIN app_auth.client_tags ct ON ct.tag_id = utl.tag_id
    GROUP BY utl.user_id
),
note_rollup AS (
    SELECT
        un.user_id,
        COUNT(*)::integer AS note_count,
        MAX(un.created_at) AS last_note_at
    FROM app_auth.user_notes un
    GROUP BY un.user_id
)
SELECT
    uas.user_id,
    uas.email,
    uas.display_name,
    uas.role_code,
    uas.status_code,
    uas.created_at,
    uas.updated_at,
    uas.last_login_at,
    uas.currency_code,
    uas.bankroll_amount,
    uas.bankroll_updated_at,
    uas.total_positions,
    uas.total_stake_amount,
    uas.last_position_at,
    uas.open_position_count,
    uas.open_position_stake,
    uas.open_golf_stake,
    uas.open_football_stake,
    uas.open_parlay_count,
    uas.open_parlay_stake,
    uas.last_parlay_at,
    uas.open_total_stake,
    uas.active_session_count,
    uas.last_seen_at,
    uas.last_session_expires_at,
    up.company_name,
    up.phone,
    up.country_code,
    up.timezone_name,
    up.segment_code,
    up.onboarding_status,
    up.source_channel,
    up.external_ref,
    pref.preferred_sport,
    pref.preferred_language,
    pref.preferred_currency,
    pref.odds_format,
    pref.timezone_name AS preference_timezone_name,
    pref.alert_opt_in,
    pref.marketing_opt_in,
    pref.automation_opt_in,
    lim.max_single_bet,
    lim.max_daily_stake,
    lim.max_open_exposure,
    lim.loss_limit_daily,
    lim.loss_limit_weekly,
    lim.requires_manual_review,
    COALESCE(tr.tag_count, 0) AS tag_count,
    COALESCE(tr.tags_csv, '') AS tags_csv,
    COALESCE(nr.note_count, 0) AS note_count,
    nr.last_note_at
FROM reporting.v_user_account_snapshot uas
LEFT JOIN app_auth.user_profiles up ON up.user_id = uas.user_id
LEFT JOIN app_auth.user_preferences pref ON pref.user_id = uas.user_id
LEFT JOIN app_auth.user_limits lim ON lim.user_id = uas.user_id
LEFT JOIN tag_rollup tr ON tr.user_id = uas.user_id
LEFT JOIN note_rollup nr ON nr.user_id = uas.user_id;

GRANT SELECT, INSERT, UPDATE, DELETE ON app_auth.user_profiles, app_auth.user_preferences, app_auth.user_limits, app_auth.client_tags, app_auth.user_tag_links, app_auth.user_notes TO spe_app_rw;
GRANT SELECT ON app_auth.user_profiles, app_auth.user_preferences, app_auth.user_limits, app_auth.client_tags, app_auth.user_tag_links, app_auth.user_notes TO spe_readonly;
GRANT SELECT ON reporting.v_user_admin_profile TO spe_app_rw, spe_readonly;
GRANT USAGE, SELECT ON SEQUENCE app_auth.client_tags_tag_id_seq TO spe_app_rw;
GRANT USAGE, SELECT ON SEQUENCE app_auth.user_notes_note_id_seq TO spe_app_rw;

COMMIT;
