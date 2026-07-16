-- 0010 - Insights de correlation (moteur de detection de correlations).
--
-- Chaque ligne est UNE correlation mesuree sur l'historique :
--   "La France avec Mbappe : 2.31 buts/match — sans lui : 1.42 (18 vs 7 matchs)"
--   "La Suisse avec < 4 jours de repos : 0.9 pts/match vs 1.8 en temps normal"
--
-- Discipline statistique integree :
--   - p_value : test statistique brut
--   - q_value : p-value corrigee des tests multiples (Benjamini-Hochberg)
--   - is_validated : q_value < seuil ET echantillons suffisants
--     -> SEULS les insights valides influencent les probabilites du moteur.
--     Les autres restent affichables a titre indicatif avec leurs effectifs.
--
-- Recalcule par run_correlation_scan.py apres chaque gros sync.

BEGIN;

CREATE TABLE IF NOT EXISTS model.correlation_insights (
    insight_id bigserial PRIMARY KEY,
    insight_code text NOT NULL CHECK (insight_code IN (
        'PLAYER_PRESENCE',   -- performance equipe avec/sans un joueur
        'SYNERGY_PAIR',      -- paire buteur-passeur recurrente
        'REST_IMPACT',       -- impact du repos court
        'H2H'                -- historique de confrontation directe
    )),
    team_id bigint NOT NULL REFERENCES core.teams(team_id) ON DELETE CASCADE,
    player_id bigint REFERENCES core.players(player_id) ON DELETE CASCADE,
    second_player_id bigint REFERENCES core.players(player_id) ON DELETE CASCADE,
    opponent_team_id bigint REFERENCES core.teams(team_id) ON DELETE CASCADE,
    subject_label text NOT NULL,          -- ex: 'France avec Mbappe'
    metric_code text NOT NULL,            -- 'points_per_match' | 'goals_per_match' | 'goal_contributions'
    effect_value numeric(8, 4) NOT NULL,  -- valeur AVEC (ou en condition)
    baseline_value numeric(8, 4),         -- valeur SANS (ou hors condition)
    sample_with integer NOT NULL,
    sample_without integer,
    p_value numeric(8, 6),
    q_value numeric(8, 6),                -- corrige FDR (Benjamini-Hochberg)
    is_validated boolean NOT NULL DEFAULT false,
    details_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    computed_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_correlation_insights_team
    ON model.correlation_insights (team_id, is_validated);
CREATE INDEX IF NOT EXISTS idx_correlation_insights_player
    ON model.correlation_insights (player_id) WHERE player_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_correlation_insights_h2h
    ON model.correlation_insights (team_id, opponent_team_id) WHERE opponent_team_id IS NOT NULL;

-- Le scan tourne cote prediction (role applicatif).
GRANT SELECT, INSERT, UPDATE, DELETE ON model.correlation_insights TO spe_app_rw;
GRANT SELECT ON model.correlation_insights TO spe_readonly;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA model TO spe_app_rw;

COMMIT;
