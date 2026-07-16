BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS model;
CREATE SCHEMA IF NOT EXISTS reporting;

CREATE TABLE IF NOT EXISTS ops.providers (
    provider_id smallserial PRIMARY KEY,
    provider_code text NOT NULL UNIQUE,
    provider_name text NOT NULL,
    base_url text NOT NULL,
    auth_mode text NOT NULL,
    sport_scope text NOT NULL,
    rate_limit_per_minute integer,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (provider_code = upper(provider_code)),
    CHECK (rate_limit_per_minute IS NULL OR rate_limit_per_minute > 0)
);

CREATE TABLE IF NOT EXISTS ops.provider_endpoints (
    endpoint_id smallserial PRIMARY KEY,
    provider_id smallint NOT NULL REFERENCES ops.providers(provider_id),
    endpoint_code text NOT NULL,
    path_template text NOT NULL,
    entity_domain text NOT NULL,
    is_enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider_id, endpoint_code)
);

CREATE TABLE IF NOT EXISTS ops.ingestion_runs (
    ingestion_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id smallint NOT NULL REFERENCES ops.providers(provider_id),
    endpoint_id smallint REFERENCES ops.provider_endpoints(endpoint_id),
    run_scope text,
    request_params jsonb NOT NULL DEFAULT '{}'::jsonb,
    requested_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    status_code text NOT NULL DEFAULT 'PENDING',
    http_status integer,
    records_received integer NOT NULL DEFAULT 0,
    records_written integer NOT NULL DEFAULT 0,
    error_message text,
    created_by text NOT NULL DEFAULT current_user,
    CHECK (status_code IN ('PENDING', 'RUNNING', 'SUCCESS', 'PARTIAL_SUCCESS', 'FAILED')),
    CHECK (records_received >= 0),
    CHECK (records_written >= 0)
);

CREATE INDEX IF NOT EXISTS idx_ingestion_runs_provider_started
    ON ops.ingestion_runs (provider_id, started_at DESC);

CREATE TABLE IF NOT EXISTS raw.provider_payloads (
    provider_payload_id bigserial PRIMARY KEY,
    provider_id smallint NOT NULL REFERENCES ops.providers(provider_id),
    endpoint_id smallint REFERENCES ops.provider_endpoints(endpoint_id),
    ingestion_run_id uuid REFERENCES ops.ingestion_runs(ingestion_run_id),
    provider_object_type text NOT NULL,
    provider_object_id text,
    natural_key text,
    payload jsonb NOT NULL,
    payload_checksum text NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT now(),
    inserted_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_provider_payloads_provider_object
    ON raw.provider_payloads (provider_id, provider_object_type, provider_object_id, captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_provider_payloads_ingestion_run
    ON raw.provider_payloads (ingestion_run_id);

CREATE INDEX IF NOT EXISTS idx_provider_payloads_payload_gin
    ON raw.provider_payloads USING gin (payload);

CREATE TABLE IF NOT EXISTS core.analysis_scopes (
    analysis_scope_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_name text NOT NULL UNIQUE,
    sport_code text NOT NULL DEFAULT 'SOCCER',
    criteria_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    active_from timestamptz,
    active_to timestamptz,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (sport_code = 'SOCCER')
);

CREATE TABLE IF NOT EXISTS core.leagues (
    league_id bigserial PRIMARY KEY,
    thesportsdb_league_id bigint NOT NULL UNIQUE,
    league_name text NOT NULL,
    alternate_name text,
    sport_name text NOT NULL,
    country_name text,
    current_season_name text,
    formed_year integer,
    gender_code text,
    description_en text,
    website_url text,
    facebook_url text,
    twitter_url text,
    youtube_url text,
    badge_url text,
    logo_url text,
    poster_url text,
    trophy_url text,
    fanart_url text,
    naming_locked boolean NOT NULL DEFAULT false,
    is_active boolean NOT NULL DEFAULT true,
    last_synced_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (sport_name = 'Soccer')
);

CREATE INDEX IF NOT EXISTS idx_leagues_country_name
    ON core.leagues (country_name, league_name);

CREATE TABLE IF NOT EXISTS core.seasons (
    season_id bigserial PRIMARY KEY,
    league_id bigint NOT NULL REFERENCES core.leagues(league_id) ON DELETE CASCADE,
    season_name text NOT NULL,
    season_label text,
    is_current boolean NOT NULL DEFAULT false,
    season_start_date date,
    season_end_date date,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (league_id, season_name)
);

CREATE INDEX IF NOT EXISTS idx_seasons_league_current
    ON core.seasons (league_id, is_current DESC, season_name DESC);

CREATE TABLE IF NOT EXISTS core.venues (
    venue_id bigserial PRIMARY KEY,
    thesportsdb_venue_id bigint UNIQUE,
    venue_name text NOT NULL,
    country_name text,
    city_name text,
    address_line text,
    capacity integer,
    surface_type text,
    description_en text,
    website_url text,
    facebook_url text,
    twitter_url text,
    instagram_url text,
    image_url text,
    thumbnail_url text,
    latitude numeric(9,6),
    longitude numeric(9,6),
    last_synced_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (capacity IS NULL OR capacity >= 0)
);

CREATE INDEX IF NOT EXISTS idx_venues_country_city
    ON core.venues (country_name, city_name, venue_name);

CREATE TABLE IF NOT EXISTS core.teams (
    team_id bigserial PRIMARY KEY,
    thesportsdb_team_id bigint NOT NULL UNIQUE,
    apifootball_team_id bigint,
    team_name text NOT NULL,
    short_name text,
    alternate_names text,
    formed_year integer,
    sport_name text NOT NULL,
    gender_code text,
    country_name text,
    stadium_name text,
    venue_id bigint REFERENCES core.venues(venue_id),
    location_name text,
    stadium_capacity integer,
    website_url text,
    facebook_url text,
    twitter_url text,
    instagram_url text,
    description_en text,
    badge_url text,
    jersey_url text,
    logo_url text,
    fanart_url text,
    is_active boolean NOT NULL DEFAULT true,
    last_synced_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (sport_name = 'Soccer'),
    CHECK (stadium_capacity IS NULL OR stadium_capacity >= 0)
);

CREATE INDEX IF NOT EXISTS idx_teams_name
    ON core.teams (team_name);

CREATE INDEX IF NOT EXISTS idx_teams_country
    ON core.teams (country_name, team_name);

CREATE TABLE IF NOT EXISTS core.team_season_memberships (
    team_season_membership_id bigserial PRIMARY KEY,
    team_id bigint NOT NULL REFERENCES core.teams(team_id) ON DELETE CASCADE,
    league_id bigint NOT NULL REFERENCES core.leagues(league_id) ON DELETE CASCADE,
    season_id bigint REFERENCES core.seasons(season_id) ON DELETE SET NULL,
    membership_role text NOT NULL DEFAULT 'PARTICIPANT',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (team_id, league_id, season_id)
);

CREATE INDEX IF NOT EXISTS idx_team_season_memberships_league
    ON core.team_season_memberships (league_id, season_id, team_id);

CREATE TABLE IF NOT EXISTS core.fixtures (
    fixture_id bigserial PRIMARY KEY,
    thesportsdb_event_id bigint NOT NULL UNIQUE,
    league_id bigint NOT NULL REFERENCES core.leagues(league_id),
    season_id bigint REFERENCES core.seasons(season_id),
    home_team_id bigint NOT NULL REFERENCES core.teams(team_id),
    away_team_id bigint NOT NULL REFERENCES core.teams(team_id),
    venue_id bigint REFERENCES core.venues(venue_id),
    event_name text NOT NULL,
    event_alternate_name text,
    filename_key text,
    kickoff_utc timestamptz,
    kickoff_local timestamp,
    event_date_utc date,
    event_time_utc time,
    event_date_local date,
    event_time_local time,
    round_number integer,
    group_label text,
    status_code text,
    status_text text,
    postponed_flag boolean NOT NULL DEFAULT false,
    locked_flag boolean NOT NULL DEFAULT false,
    official_name text,
    weather_text text,
    attendance integer,
    country_name text,
    city_name text,
    video_url text,
    highlights_url text,
    raw_last_snapshot_at timestamptz,
    last_synced_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (home_team_id <> away_team_id),
    CHECK (attendance IS NULL OR attendance >= 0),
    CHECK (round_number IS NULL OR round_number >= 0)
);

CREATE INDEX IF NOT EXISTS idx_fixtures_league_kickoff
    ON core.fixtures (league_id, kickoff_utc DESC);

CREATE INDEX IF NOT EXISTS idx_fixtures_season_kickoff
    ON core.fixtures (season_id, kickoff_utc DESC);

CREATE INDEX IF NOT EXISTS idx_fixtures_status_kickoff
    ON core.fixtures (status_code, kickoff_utc DESC);

CREATE TABLE IF NOT EXISTS core.fixture_scores (
    fixture_id bigint PRIMARY KEY REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    home_score smallint,
    away_score smallint,
    home_score_extra smallint,
    away_score_extra smallint,
    winner_code text,
    score_status text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (home_score IS NULL OR home_score >= 0),
    CHECK (away_score IS NULL OR away_score >= 0),
    CHECK (home_score_extra IS NULL OR home_score_extra >= 0),
    CHECK (away_score_extra IS NULL OR away_score_extra >= 0),
    CHECK (winner_code IS NULL OR winner_code IN ('HOME', 'DRAW', 'AWAY', 'UNKNOWN'))
);

CREATE TABLE IF NOT EXISTS core.fixture_lineups (
    fixture_lineup_id bigserial PRIMARY KEY,
    fixture_id bigint NOT NULL REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    team_id bigint REFERENCES core.teams(team_id) ON DELETE SET NULL,
    lineup_role text NOT NULL,
    player_name text NOT NULL,
    player_position text,
    jersey_number smallint,
    sort_order smallint,
    captain_flag boolean NOT NULL DEFAULT false,
    raw_item jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (fixture_id, team_id, lineup_role, player_name, sort_order),
    CHECK (lineup_role IN ('STARTER', 'SUBSTITUTE', 'COACH', 'UNKNOWN'))
);

CREATE INDEX IF NOT EXISTS idx_fixture_lineups_fixture_team
    ON core.fixture_lineups (fixture_id, team_id);

CREATE TABLE IF NOT EXISTS core.fixture_timeline_events (
    fixture_timeline_event_id bigserial PRIMARY KEY,
    fixture_id bigint NOT NULL REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    team_id bigint REFERENCES core.teams(team_id) ON DELETE SET NULL,
    player_name text,
    assistant_name text,
    minute_mark smallint,
    period_code text,
    event_type text NOT NULL,
    detail_text text,
    sort_order smallint,
    raw_item jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (minute_mark IS NULL OR minute_mark >= 0)
);

CREATE INDEX IF NOT EXISTS idx_fixture_timeline_events_fixture
    ON core.fixture_timeline_events (fixture_id, sort_order, minute_mark);

CREATE TABLE IF NOT EXISTS core.fixture_team_statistics (
    fixture_team_statistic_id bigserial PRIMARY KEY,
    fixture_id bigint NOT NULL REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    team_id bigint NOT NULL REFERENCES core.teams(team_id) ON DELETE CASCADE,
    stat_name text NOT NULL,
    stat_value_numeric numeric(14,4),
    stat_value_text text,
    captured_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (fixture_id, team_id, stat_name)
);

CREATE INDEX IF NOT EXISTS idx_fixture_team_statistics_fixture
    ON core.fixture_team_statistics (fixture_id, team_id);

CREATE TABLE IF NOT EXISTS core.league_table_snapshots (
    league_table_snapshot_id bigserial PRIMARY KEY,
    league_id bigint NOT NULL REFERENCES core.leagues(league_id) ON DELETE CASCADE,
    season_id bigint REFERENCES core.seasons(season_id) ON DELETE SET NULL,
    captured_at timestamptz NOT NULL DEFAULT now(),
    source_run_id uuid REFERENCES ops.ingestion_runs(ingestion_run_id),
    UNIQUE (league_id, season_id, captured_at)
);

CREATE TABLE IF NOT EXISTS core.league_table_rows (
    league_table_row_id bigserial PRIMARY KEY,
    league_table_snapshot_id bigint NOT NULL REFERENCES core.league_table_snapshots(league_table_snapshot_id) ON DELETE CASCADE,
    team_id bigint NOT NULL REFERENCES core.teams(team_id) ON DELETE CASCADE,
    position_number integer NOT NULL,
    matches_played integer,
    wins integer,
    draws integer,
    losses integer,
    goals_for integer,
    goals_against integer,
    goal_difference integer,
    points integer,
    form_text text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (league_table_snapshot_id, team_id),
    UNIQUE (league_table_snapshot_id, position_number),
    CHECK (position_number > 0)
);

CREATE INDEX IF NOT EXISTS idx_league_table_rows_snapshot_position
    ON core.league_table_rows (league_table_snapshot_id, position_number);

CREATE TABLE IF NOT EXISTS core.bookmakers (
    bookmaker_id bigserial PRIMARY KEY,
    bookmaker_code text NOT NULL UNIQUE,
    bookmaker_name text NOT NULL,
    website_url text,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (bookmaker_code = upper(bookmaker_code))
);

CREATE TABLE IF NOT EXISTS core.fixture_odds_1x2 (
    fixture_odd_id bigserial PRIMARY KEY,
    fixture_id bigint NOT NULL REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    bookmaker_id bigint NOT NULL REFERENCES core.bookmakers(bookmaker_id) ON DELETE CASCADE,
    captured_at timestamptz NOT NULL,
    home_odd numeric(10,4) NOT NULL,
    draw_odd numeric(10,4) NOT NULL,
    away_odd numeric(10,4) NOT NULL,
    is_closing_line boolean NOT NULL DEFAULT false,
    source_system text NOT NULL DEFAULT 'EXTERNAL_ODDS_PROVIDER',
    source_reference text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (fixture_id, bookmaker_id, captured_at),
    CHECK (home_odd > 1.0000),
    CHECK (draw_odd > 1.0000),
    CHECK (away_odd > 1.0000)
);

CREATE INDEX IF NOT EXISTS idx_fixture_odds_1x2_fixture_captured
    ON core.fixture_odds_1x2 (fixture_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS model.model_runs (
    model_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_scope_id uuid REFERENCES core.analysis_scopes(analysis_scope_id) ON DELETE SET NULL,
    model_name text NOT NULL,
    model_version text NOT NULL,
    run_type text NOT NULL,
    status_code text NOT NULL DEFAULT 'PENDING',
    training_window_start date,
    training_window_end date,
    parameters_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    metrics_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz,
    finished_at timestamptz,
    notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (run_type IN ('TRAIN', 'INFERENCE', 'BACKTEST')),
    CHECK (status_code IN ('PENDING', 'RUNNING', 'SUCCESS', 'FAILED'))
);

CREATE INDEX IF NOT EXISTS idx_model_runs_model_version
    ON model.model_runs (model_name, model_version, created_at DESC);

CREATE TABLE IF NOT EXISTS model.predictions (
    prediction_id bigserial PRIMARY KEY,
    fixture_id bigint NOT NULL REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    model_run_id uuid REFERENCES model.model_runs(model_run_id) ON DELETE SET NULL,
    model_name text NOT NULL,
    model_version text NOT NULL,
    generated_at timestamptz NOT NULL DEFAULT now(),
    home_win_probability numeric(8,6) NOT NULL,
    draw_probability numeric(8,6) NOT NULL,
    away_win_probability numeric(8,6) NOT NULL,
    expected_home_goals numeric(8,4),
    expected_away_goals numeric(8,4),
    confidence_score numeric(8,6),
    feature_snapshot_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (home_win_probability >= 0 AND home_win_probability <= 1),
    CHECK (draw_probability >= 0 AND draw_probability <= 1),
    CHECK (away_win_probability >= 0 AND away_win_probability <= 1),
    CHECK (confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)),
    CHECK ((home_win_probability + draw_probability + away_win_probability) BETWEEN 0.990000 AND 1.010000)
);

CREATE INDEX IF NOT EXISTS idx_predictions_fixture_generated
    ON model.predictions (fixture_id, generated_at DESC);

CREATE INDEX IF NOT EXISTS idx_predictions_model_generated
    ON model.predictions (model_name, model_version, generated_at DESC);

CREATE TABLE IF NOT EXISTS model.value_bets (
    value_bet_id bigserial PRIMARY KEY,
    fixture_id bigint NOT NULL REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    prediction_id bigint NOT NULL REFERENCES model.predictions(prediction_id) ON DELETE CASCADE,
    bookmaker_id bigint REFERENCES core.bookmakers(bookmaker_id) ON DELETE SET NULL,
    selection_code text NOT NULL,
    market_code text NOT NULL DEFAULT '1X2',
    model_probability numeric(8,6) NOT NULL,
    implied_probability numeric(8,6) NOT NULL,
    edge_probability numeric(8,6) NOT NULL,
    fair_odd numeric(10,4),
    market_odd numeric(10,4),
    detected_at timestamptz NOT NULL DEFAULT now(),
    status_code text NOT NULL DEFAULT 'ACTIVE',
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (selection_code IN ('HOME', 'DRAW', 'AWAY')),
    CHECK (market_code = '1X2'),
    CHECK (model_probability >= 0 AND model_probability <= 1),
    CHECK (implied_probability >= 0 AND implied_probability <= 1),
    CHECK (fair_odd IS NULL OR fair_odd > 1.0000),
    CHECK (market_odd IS NULL OR market_odd > 1.0000)
);

CREATE INDEX IF NOT EXISTS idx_value_bets_fixture_detected
    ON model.value_bets (fixture_id, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_value_bets_selection_edge
    ON model.value_bets (selection_code, edge_probability DESC, detected_at DESC);

CREATE TABLE IF NOT EXISTS model.deal_rankings (
    deal_ranking_id bigserial PRIMARY KEY,
    analysis_scope_id uuid NOT NULL REFERENCES core.analysis_scopes(analysis_scope_id) ON DELETE CASCADE,
    fixture_id bigint NOT NULL REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    prediction_id bigint NOT NULL REFERENCES model.predictions(prediction_id) ON DELETE CASCADE,
    value_bet_id bigint REFERENCES model.value_bets(value_bet_id) ON DELETE SET NULL,
    ranking_score numeric(8,6) NOT NULL,
    confidence_score numeric(8,6),
    data_quality_score numeric(8,6),
    freshness_score numeric(8,6),
    rank_position integer NOT NULL,
    summary_reason text,
    generated_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (analysis_scope_id, generated_at, rank_position),
    CHECK (ranking_score >= 0 AND ranking_score <= 1),
    CHECK (confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)),
    CHECK (data_quality_score IS NULL OR (data_quality_score >= 0 AND data_quality_score <= 1)),
    CHECK (freshness_score IS NULL OR (freshness_score >= 0 AND freshness_score <= 1)),
    CHECK (rank_position > 0)
);

CREATE INDEX IF NOT EXISTS idx_deal_rankings_scope_generated
    ON model.deal_rankings (analysis_scope_id, generated_at DESC, rank_position ASC);

CREATE OR REPLACE VIEW reporting.v_latest_predictions_1x2 AS
WITH ranked_predictions AS (
    SELECT
        p.*,
        row_number() OVER (
            PARTITION BY p.fixture_id
            ORDER BY p.generated_at DESC, p.prediction_id DESC
        ) AS row_num
    FROM model.predictions p
)
SELECT
    prediction_id,
    fixture_id,
    model_run_id,
    model_name,
    model_version,
    generated_at,
    home_win_probability,
    draw_probability,
    away_win_probability,
    expected_home_goals,
    expected_away_goals,
    confidence_score
FROM ranked_predictions
WHERE row_num = 1;

CREATE OR REPLACE VIEW reporting.v_match_board_1x2 AS
SELECT
    f.fixture_id,
    f.thesportsdb_event_id,
    l.league_name,
    s.season_name,
    home_team.team_name AS home_team_name,
    away_team.team_name AS away_team_name,
    f.kickoff_utc,
    f.status_code,
    fs.home_score,
    fs.away_score,
    p.prediction_id,
    p.model_name,
    p.model_version,
    p.generated_at AS prediction_generated_at,
    p.home_win_probability,
    p.draw_probability,
    p.away_win_probability,
    p.expected_home_goals,
    p.expected_away_goals,
    p.confidence_score
FROM core.fixtures f
JOIN core.leagues l
    ON l.league_id = f.league_id
LEFT JOIN core.seasons s
    ON s.season_id = f.season_id
JOIN core.teams home_team
    ON home_team.team_id = f.home_team_id
JOIN core.teams away_team
    ON away_team.team_id = f.away_team_id
LEFT JOIN core.fixture_scores fs
    ON fs.fixture_id = f.fixture_id
LEFT JOIN reporting.v_latest_predictions_1x2 p
    ON p.fixture_id = f.fixture_id;

CREATE OR REPLACE VIEW reporting.v_latest_odds_1x2 AS
WITH ranked_odds AS (
    SELECT
        fo.*,
        row_number() OVER (
            PARTITION BY fo.fixture_id, fo.bookmaker_id
            ORDER BY fo.captured_at DESC, fo.fixture_odd_id DESC
        ) AS row_num
    FROM core.fixture_odds_1x2 fo
)
SELECT
    fixture_odd_id,
    fixture_id,
    bookmaker_id,
    captured_at,
    home_odd,
    draw_odd,
    away_odd,
    is_closing_line,
    source_system,
    source_reference
FROM ranked_odds
WHERE row_num = 1;

CREATE OR REPLACE VIEW reporting.v_ranked_deals_1x2 AS
SELECT
    dr.deal_ranking_id,
    dr.analysis_scope_id,
    dr.rank_position,
    dr.ranking_score,
    dr.confidence_score,
    dr.data_quality_score,
    dr.freshness_score,
    dr.summary_reason,
    dr.generated_at,
    f.fixture_id,
    f.thesportsdb_event_id,
    f.kickoff_utc,
    l.league_name,
    home_team.team_name AS home_team_name,
    away_team.team_name AS away_team_name,
    vb.selection_code,
    vb.market_code,
    vb.model_probability,
    vb.implied_probability,
    vb.edge_probability,
    vb.fair_odd,
    vb.market_odd,
    b.bookmaker_name
FROM model.deal_rankings dr
JOIN core.fixtures f
    ON f.fixture_id = dr.fixture_id
JOIN core.leagues l
    ON l.league_id = f.league_id
JOIN core.teams home_team
    ON home_team.team_id = f.home_team_id
JOIN core.teams away_team
    ON away_team.team_id = f.away_team_id
LEFT JOIN model.value_bets vb
    ON vb.value_bet_id = dr.value_bet_id
LEFT JOIN core.bookmakers b
    ON b.bookmaker_id = vb.bookmaker_id;

INSERT INTO ops.providers (
    provider_code,
    provider_name,
    base_url,
    auth_mode,
    sport_scope,
    rate_limit_per_minute
)
VALUES (
    'THESPORTSDB',
    'TheSportsDB',
    'https://www.thesportsdb.com/api/v1/json',
    'URL_API_KEY',
    'SOCCER',
    30
)
ON CONFLICT (provider_code) DO UPDATE
SET
    provider_name = EXCLUDED.provider_name,
    base_url = EXCLUDED.base_url,
    auth_mode = EXCLUDED.auth_mode,
    sport_scope = EXCLUDED.sport_scope,
    rate_limit_per_minute = EXCLUDED.rate_limit_per_minute,
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
        ('ALL_LEAGUES', '/123/all_leagues.php', 'LEAGUE'),
        ('SEARCH_ALL_LEAGUES', '/123/search_all_leagues.php?c={country}&s=Soccer', 'LEAGUE'),
        ('LIST_SEASONS', '/123/search_all_seasons.php?id={idLeague}', 'SEASON'),
        ('SEARCH_ALL_TEAMS', '/123/search_all_teams.php?l={league}', 'TEAM'),
        ('LOOKUP_TEAM', '/123/lookupteam.php?id={idTeam}', 'TEAM'),
        ('LOOKUP_VENUE', '/123/lookupvenue.php?id={idVenue}', 'VENUE'),
        ('EVENTS_SEASON', '/123/eventsseason.php?id={idLeague}&s={season}', 'FIXTURE'),
        ('EVENTS_NEXT_LEAGUE', '/123/eventsnextleague.php?id={idLeague}', 'FIXTURE'),
        ('EVENTS_PAST_LEAGUE', '/123/eventspastleague.php?id={idLeague}', 'FIXTURE'),
        ('EVENTS_DAY', '/123/eventsday.php?d={date}&s=Soccer', 'FIXTURE'),
        ('LOOKUP_EVENT', '/123/lookupevent.php?id={idEvent}', 'FIXTURE'),
        ('LOOKUP_EVENT_STATS', '/123/lookupeventstats.php?id={idEvent}', 'FIXTURE_STATS'),
        ('LOOKUP_LINEUP', '/123/lookuplineup.php?id={idEvent}', 'FIXTURE_LINEUP'),
        ('LOOKUP_TIMELINE', '/123/lookuptimeline.php?id={idEvent}', 'FIXTURE_TIMELINE'),
        ('LOOKUP_TABLE', '/123/lookuptable.php?l={idLeague}&s={season}', 'LEAGUE_TABLE')
) AS endpoint_defs(endpoint_code, path_template, entity_domain)
WHERE p.provider_code = 'THESPORTSDB'
ON CONFLICT (provider_id, endpoint_code) DO UPDATE
SET
    path_template = EXCLUDED.path_template,
    entity_domain = EXCLUDED.entity_domain,
    is_enabled = true;

COMMIT;
