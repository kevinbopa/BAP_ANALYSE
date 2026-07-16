-- 0008 - Couche joueurs (API-Football) + fondation contexte extra-sportif.
--
-- Objectif :
--   1. Stocker joueurs, stats par match, blessures et effectifs de competition
--      pour alimenter les signaux "disponibilite" et "fatigue reelle" du moteur.
--   2. Poser la table generique des facteurs extra-sportifs (meteo, enjeu,
--      deplacement, arbitre...) consommee par spe_prediction.context.
--
-- Idempotente (IF NOT EXISTS / ON CONFLICT), s'appuie sur les roles de 0002.

BEGIN;

-- ---------------------------------------------------------------------------
-- Provider API-Football
-- ---------------------------------------------------------------------------
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
    'APIFOOTBALL',
    'API-Football (api-sports.io)',
    'https://v3.football.api-sports.io',
    'HEADER_API_KEY',
    'SOCCER_PLAYERS',
    10,
    true
)
ON CONFLICT (provider_code) DO UPDATE
SET
    provider_name = EXCLUDED.provider_name,
    base_url = EXCLUDED.base_url,
    auth_mode = EXCLUDED.auth_mode,
    sport_scope = EXCLUDED.sport_scope,
    rate_limit_per_minute = EXCLUDED.rate_limit_per_minute,
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
    endpoint_defs.endpoint_code,
    endpoint_defs.path_template,
    endpoint_defs.entity_domain,
    true
FROM ops.providers p
CROSS JOIN (
    VALUES
        ('SQUADS', '/players/squads', 'PLAYER'),
        ('INJURIES', '/injuries', 'PLAYER_INJURY'),
        ('FIXTURE_LINEUPS', '/fixtures/lineups', 'LINEUP'),
        ('FIXTURE_PLAYERS', '/fixtures/players', 'PLAYER_MATCH_STATS')
) AS endpoint_defs(endpoint_code, path_template, entity_domain)
WHERE p.provider_code = 'APIFOOTBALL'
ON CONFLICT (provider_id, endpoint_code) DO UPDATE
SET
    path_template = EXCLUDED.path_template,
    entity_domain = EXCLUDED.entity_domain,
    is_enabled = true;

-- ---------------------------------------------------------------------------
-- Joueurs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.players (
    player_id bigserial PRIMARY KEY,
    apifootball_player_id bigint UNIQUE,
    player_name text NOT NULL,
    firstname text,
    lastname text,
    birth_date date,
    nationality text,
    height_cm integer,
    weight_kg integer,
    position_code text,          -- G / D / M / F
    photo_url text,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_synced_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_players_name ON core.players (player_name);

-- Effectif d'une equipe pour une competition/saison (liste des selectionnes)
CREATE TABLE IF NOT EXISTS core.team_squad_members (
    squad_member_id bigserial PRIMARY KEY,
    team_id bigint NOT NULL REFERENCES core.teams(team_id) ON DELETE CASCADE,
    player_id bigint NOT NULL REFERENCES core.players(player_id) ON DELETE CASCADE,
    season_year integer,
    competition_name text,
    shirt_number integer,
    position_code text,
    is_captain boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (team_id, player_id, season_year, competition_name)
);

CREATE INDEX IF NOT EXISTS idx_squad_members_team ON core.team_squad_members (team_id, season_year);

-- Stats d'un joueur sur un match (minutes, note, position)
CREATE TABLE IF NOT EXISTS core.player_match_stats (
    player_match_stat_id bigserial PRIMARY KEY,
    fixture_id bigint REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    apifootball_fixture_id bigint,
    player_id bigint NOT NULL REFERENCES core.players(player_id) ON DELETE CASCADE,
    team_id bigint REFERENCES core.teams(team_id) ON DELETE SET NULL,
    kickoff_utc timestamptz,
    minutes_played integer,
    rating numeric(4, 2),          -- note du match (echelle 0-10 API-Football)
    position_code text,
    is_starter boolean,
    is_captain boolean NOT NULL DEFAULT false,
    goals integer,
    assists integer,
    shots_total integer,
    shots_on_target integer,
    passes_total integer,
    passes_accuracy numeric(5, 2),
    tackles_total integer,
    interceptions integer,
    duels_total integer,
    duels_won integer,
    dribbles_success integer,
    fouls_committed integer,
    yellow_cards integer,
    red_cards integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (player_id, apifootball_fixture_id)
);

CREATE INDEX IF NOT EXISTS idx_player_match_stats_player_kickoff
    ON core.player_match_stats (player_id, kickoff_utc DESC);
CREATE INDEX IF NOT EXISTS idx_player_match_stats_team
    ON core.player_match_stats (team_id, kickoff_utc DESC);

-- Blessures / indisponibilites
CREATE TABLE IF NOT EXISTS core.player_injuries (
    player_injury_id bigserial PRIMARY KEY,
    player_id bigint NOT NULL REFERENCES core.players(player_id) ON DELETE CASCADE,
    team_id bigint REFERENCES core.teams(team_id) ON DELETE SET NULL,
    injury_type text,              -- ex: 'Muscle Injury', 'Knee Injury', 'Suspended'
    injury_reason text,
    reported_at timestamptz,
    fixture_apifootball_id bigint, -- fixture concernee si fournie par l'API
    season_year integer,
    is_active boolean NOT NULL DEFAULT true,
    resolved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_player_injuries_player
    ON core.player_injuries (player_id, reported_at DESC);
CREATE INDEX IF NOT EXISTS idx_player_injuries_team_active
    ON core.player_injuries (team_id) WHERE is_active;

-- ---------------------------------------------------------------------------
-- Fondation extra-sportive : facteurs de contexte par fixture
-- ---------------------------------------------------------------------------
-- Chaque ligne est UN facteur type pour UN match, value normalisee [-1, +1]
-- du point de vue de l'equipe domicile (+1 = tres favorable au domicile).
-- Consommee par spe_prediction.context qui la convertit en deltas xG/Elo.
CREATE TABLE IF NOT EXISTS core.fixture_context_factors (
    context_factor_id bigserial PRIMARY KEY,
    fixture_id bigint NOT NULL REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    factor_code text NOT NULL,     -- WEATHER / STAKES / TRAVEL / REFEREE / CROWD / NEWS
    factor_value numeric(4, 3) NOT NULL CHECK (factor_value BETWEEN -1 AND 1),
    weight numeric(4, 3) NOT NULL DEFAULT 1.0 CHECK (weight BETWEEN 0 AND 1),
    source_code text NOT NULL DEFAULT 'MANUAL',   -- MANUAL / API / MODEL
    note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (fixture_id, factor_code, source_code)
);

CREATE INDEX IF NOT EXISTS idx_fixture_context_factors_fixture
    ON core.fixture_context_factors (fixture_id);

-- ---------------------------------------------------------------------------
-- Droits (memes conventions que 0002) : l'ingestion ecrit, l'app lit.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON core.players,
                                core.team_squad_members,
                                core.player_match_stats,
                                core.player_injuries,
                                core.fixture_context_factors
    TO spe_ingest_rw;
GRANT SELECT ON core.players,
                core.team_squad_members,
                core.player_match_stats,
                core.player_injuries,
                core.fixture_context_factors
    TO spe_app_rw, spe_readonly;
-- L'app peut saisir des facteurs manuels depuis le dashboard.
GRANT INSERT, UPDATE ON core.fixture_context_factors TO spe_app_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA core TO spe_ingest_rw, spe_app_rw;

COMMIT;
