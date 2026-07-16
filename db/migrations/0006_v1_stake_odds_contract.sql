BEGIN;

UPDATE ops.providers
SET
    provider_name = 'Stake Odds Data',
    base_url = 'https://odds-data.stake.com',
    auth_mode = 'OPTIONAL_API_KEY_HEADER',
    sport_scope = 'SOCCER_ODDS',
    updated_at = now()
WHERE provider_code = 'STAKE';

DELETE FROM ops.provider_endpoints
WHERE provider_id = (
    SELECT provider_id
    FROM ops.providers
    WHERE provider_code = 'STAKE'
)
AND endpoint_code IN (
    'SPORTSBOOK_ODDS_1X2',
    'SPORTSBOOK_FIXTURE_LOOKUP',
    'SPORTSBOOK_COMPETITIONS'
);

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
        ('SPORTSBOOK_SPORTS', '/sports', 'SPORT'),
        ('SPORTSBOOK_CATEGORIES', '/sports/{sport}/categories', 'CATEGORY'),
        ('SPORTSBOOK_TOURNAMENTS', '/sports/{sport}/{category}/tournaments', 'TOURNAMENT'),
        ('SPORTSBOOK_TOURNAMENT_FIXTURES', '/sports/{sport}/{category}/{tournament}/fixtures', 'FIXTURE'),
        ('SPORTSBOOK_FIXTURE_DETAILS', '/fixtures/{fixture_slug}', 'ODDS_FIXTURE'),
        ('SPORTSBOOK_FIXTURE_ODDS', '/odds/{fixture_slug}', 'ODDS_FIXTURE')
) AS endpoint_defs(endpoint_code, path_template, entity_domain)
WHERE p.provider_code = 'STAKE'
ON CONFLICT (provider_id, endpoint_code) DO UPDATE
SET
    path_template = EXCLUDED.path_template,
    entity_domain = EXCLUDED.entity_domain,
    is_enabled = true;

COMMIT;
