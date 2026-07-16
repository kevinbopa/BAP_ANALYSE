-- 0009 - Donnees joueurs par match (TheSportsDB Premium).
--
-- Le gisement des correlations joueur : qui a joue chaque match (lineups),
-- chaque but/carton/remplacement avec buteur et passeur (timeline), et les
-- stats d'equipe par match (tirs, possession...).
--
-- Inclut la table de progression du backfill : le remplissage de ~28K
-- fixtures prend des heures et doit etre interruptible/reprenable.
--
-- Idempotente. S'appuie sur les roles de 0002 et les tables de 0008.

BEGIN;

-- ---------------------------------------------------------------------------
-- Pont TheSportsDB sur les joueurs (0008 n'avait que le pont API-Football)
-- ---------------------------------------------------------------------------
ALTER TABLE core.players
    ADD COLUMN IF NOT EXISTS thesportsdb_player_id bigint;

CREATE UNIQUE INDEX IF NOT EXISTS uq_players_thesportsdb_id
    ON core.players (thesportsdb_player_id)
    WHERE thesportsdb_player_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Compositions par match
--
-- La baseline 0001 contenait un placeholder core.fixture_lineups base sur les
-- NOMS de joueurs (aucune identite joueur -> inutilisable pour les
-- correlations). Jamais alimente par aucun ingesteur. On le remplace par le
-- schema a cle etrangere player_id — uniquement s'il est VIDE (garde-fou).
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'core' AND table_name = 'fixture_lineups'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'core' AND table_name = 'fixture_lineups'
          AND column_name = 'player_id'
    ) THEN
        IF (SELECT COUNT(*) FROM core.fixture_lineups) > 0 THEN
            RAISE EXCEPTION 'core.fixture_lineups (ancien schema par noms) contient des lignes - migration manuelle requise';
        END IF;
        DROP TABLE core.fixture_lineups;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS core.fixture_lineups (
    fixture_lineup_id bigserial PRIMARY KEY,
    fixture_id bigint NOT NULL REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    player_id bigint NOT NULL REFERENCES core.players(player_id) ON DELETE CASCADE,
    team_id bigint REFERENCES core.teams(team_id) ON DELETE SET NULL,
    is_home boolean,
    is_starter boolean NOT NULL DEFAULT true,
    position_code text,
    shirt_number integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (fixture_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_fixture_lineups_player
    ON core.fixture_lineups (player_id, fixture_id);
CREATE INDEX IF NOT EXISTS idx_fixture_lineups_fixture
    ON core.fixture_lineups (fixture_id);

-- ---------------------------------------------------------------------------
-- Timeline des matchs : buts (avec passeur), cartons, remplacements
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.fixture_timeline (
    fixture_timeline_id bigserial PRIMARY KEY,
    thesportsdb_timeline_id bigint UNIQUE,
    fixture_id bigint NOT NULL REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    event_code text NOT NULL,          -- GOAL / CARD / SUB / VAR / OTHER
    event_detail text,                 -- 'Normal Goal', 'Yellow Card', 'Penalty'...
    minute integer,
    is_home boolean,
    player_id bigint REFERENCES core.players(player_id) ON DELETE SET NULL,
    assist_player_id bigint REFERENCES core.players(player_id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fixture_timeline_fixture
    ON core.fixture_timeline (fixture_id);
CREATE INDEX IF NOT EXISTS idx_fixture_timeline_player
    ON core.fixture_timeline (player_id) WHERE player_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_fixture_timeline_assist
    ON core.fixture_timeline (assist_player_id) WHERE assist_player_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Stats d'equipe par match (tirs, possession, corners...)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.fixture_team_stats (
    fixture_team_stat_id bigserial PRIMARY KEY,
    fixture_id bigint NOT NULL REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    stat_name text NOT NULL,
    home_value numeric(8, 2),
    away_value numeric(8, 2),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (fixture_id, stat_name)
);

CREATE INDEX IF NOT EXISTS idx_fixture_team_stats_fixture
    ON core.fixture_team_stats (fixture_id);

-- ---------------------------------------------------------------------------
-- Progression du backfill : interruptible et reprenable
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.player_backfill_progress (
    fixture_id bigint PRIMARY KEY REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    status_code text NOT NULL DEFAULT 'DONE'
        CHECK (status_code IN ('DONE', 'NO_DATA', 'ERROR')),
    lineup_rows integer NOT NULL DEFAULT 0,
    timeline_rows integer NOT NULL DEFAULT 0,
    stat_rows integer NOT NULL DEFAULT 0,
    error_message text,
    processed_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Droits (conventions 0002) : ingestion ecrit, app lit.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON core.fixture_lineups,
                                core.fixture_timeline,
                                core.fixture_team_stats,
                                ops.player_backfill_progress
    TO spe_ingest_rw;
GRANT SELECT ON core.fixture_lineups,
                core.fixture_timeline,
                core.fixture_team_stats,
                ops.player_backfill_progress
    TO spe_app_rw, spe_readonly;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA core TO spe_ingest_rw, spe_app_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ops TO spe_ingest_rw, spe_app_rw;

COMMIT;
