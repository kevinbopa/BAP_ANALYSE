BEGIN;

INSERT INTO ops.providers (
    provider_code,
    provider_name,
    base_url,
    auth_mode,
    sport_scope,
    rate_limit_per_minute,
    is_active
)
VALUES (
    'THEODDSAPI',
    'The Odds API',
    'https://api.the-odds-api.com/v4',
    'QUERY_API_KEY',
    'SOCCER_ODDS',
    NULL,
    true
)
ON CONFLICT (provider_code) DO UPDATE
SET
    provider_name = EXCLUDED.provider_name,
    base_url = EXCLUDED.base_url,
    auth_mode = EXCLUDED.auth_mode,
    sport_scope = EXCLUDED.sport_scope,
    is_active = true,
    updated_at = now();

INSERT INTO ops.provider_endpoints (
    provider_id,
    endpoint_code,
    path_template,
    entity_domain,
    is_enabled
)
SELECT
    p.provider_id,
    endpoint_code,
    path_template,
    entity_domain,
    true
FROM ops.providers p
CROSS JOIN (
    VALUES
        ('SPORTS', '/sports', 'SPORT'),
        ('CURRENT_ODDS_H2H', '/sports/{sport_key}/odds', 'ODDS_EVENT')
) AS endpoint_defs(endpoint_code, path_template, entity_domain)
WHERE p.provider_code = 'THEODDSAPI'
ON CONFLICT (provider_id, endpoint_code) DO UPDATE
SET
    path_template = EXCLUDED.path_template,
    entity_domain = EXCLUDED.entity_domain,
    is_enabled = true;

COMMIT;
