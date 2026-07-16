BEGIN;

INSERT INTO ops.providers (
    provider_code,
    provider_name,
    base_url,
    auth_mode,
    sport_scope,
    rate_limit_per_minute
)
VALUES (
    'STAKE',
    'Stake',
    'https://stake.com',
    'TOKEN_HEADER',
    'SOCCER_ODDS',
    NULL
)
ON CONFLICT (provider_code) DO UPDATE
SET
    provider_name = EXCLUDED.provider_name,
    base_url = EXCLUDED.base_url,
    auth_mode = EXCLUDED.auth_mode,
    sport_scope = EXCLUDED.sport_scope,
    updated_at = now();

INSERT INTO ops.provider_endpoints (
    provider_id,
    endpoint_code,
    path_template,
    entity_domain
)
SELECT
    p.provider_id,
    endpoint_code,
    path_template,
    entity_domain
FROM ops.providers p
CROSS JOIN (
    VALUES
        ('SPORTSBOOK_ODDS_1X2', '{configured_at_runtime}', 'ODDS_1X2'),
        ('SPORTSBOOK_FIXTURE_LOOKUP', '{configured_at_runtime}', 'ODDS_FIXTURE'),
        ('SPORTSBOOK_COMPETITIONS', '{configured_at_runtime}', 'ODDS_COMPETITION')
) AS endpoint_defs(endpoint_code, path_template, entity_domain)
WHERE p.provider_code = 'STAKE'
ON CONFLICT (provider_id, endpoint_code) DO UPDATE
SET
    path_template = EXCLUDED.path_template,
    entity_domain = EXCLUDED.entity_domain,
    is_enabled = true;

COMMIT;
