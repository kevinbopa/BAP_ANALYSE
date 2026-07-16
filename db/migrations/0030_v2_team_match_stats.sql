-- 0030 : stats de match par equipe (API-Football) — surtout le xG.
--
-- Le xG (expected goals) est un bien meilleur predicteur des buts futurs que
-- les buts bruts (moins de bruit). Lu par equipe via apifootball_team_id (deja
-- bridge) -> pas besoin de pont fixture. Bonus : corners + cartons (pour de
-- futurs marches). Le xG API-Football existe pour les grandes ligues ~2022+.

BEGIN;

CREATE TABLE IF NOT EXISTS core.team_match_stats (
    team_match_stat_id bigserial PRIMARY KEY,
    apifootball_fixture_id bigint NOT NULL,
    apifootball_team_id bigint NOT NULL,
    team_id bigint REFERENCES core.teams(team_id) ON DELETE SET NULL,
    kickoff_utc timestamptz,
    league_name text,
    is_home boolean,
    goals_for integer,
    goals_against integer,
    xg_for numeric(6, 3),
    xg_against numeric(6, 3),
    shots_total integer,
    shots_on integer,
    corners integer,
    yellow_cards integer,
    red_cards integer,
    possession numeric(5, 2),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (apifootball_fixture_id, apifootball_team_id)
);

CREATE INDEX IF NOT EXISTS idx_team_match_stats_team_date
    ON core.team_match_stats (team_id, kickoff_utc DESC);
CREATE INDEX IF NOT EXISTS idx_team_match_stats_afteam_date
    ON core.team_match_stats (apifootball_team_id, kickoff_utc DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON core.team_match_stats TO spe_ingest_rw;
GRANT SELECT ON core.team_match_stats TO spe_app_rw, spe_readonly;
GRANT USAGE, SELECT ON SEQUENCE core.team_match_stats_team_match_stat_id_seq TO spe_ingest_rw;

COMMIT;
