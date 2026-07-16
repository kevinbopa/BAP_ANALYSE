-- 0020 : combines pris (paris a plusieurs jambes).
--
-- Un combine gagne UNIQUEMENT si toutes ses jambes gagnent (produit des
-- cotes). Le profit reel entre dans l'analyse de performance au meme titre
-- que les paris simples. Chaque jambe se regle sur les 90 minutes
-- reglementaires de son match (meme logique que les value bets).

BEGIN;

CREATE TABLE IF NOT EXISTS model.parlay_tickets (
    parlay_id bigserial PRIMARY KEY,
    combined_odd numeric(12, 4) NOT NULL,
    stake_amount numeric(12, 4) NOT NULL,
    leg_count integer NOT NULL,
    source text NOT NULL DEFAULT 'STRATEGIE',
    label text,
    note text,
    taken_at timestamptz NOT NULL DEFAULT now(),
    result_code text,
    profit_units numeric(12, 4),
    settled_at timestamptz,
    CHECK (combined_odd > 1),
    CHECK (stake_amount > 0),
    CHECK (leg_count >= 2),
    CHECK (result_code IS NULL OR result_code IN ('WON', 'LOST', 'VOID'))
);

CREATE TABLE IF NOT EXISTS model.parlay_legs (
    leg_id bigserial PRIMARY KEY,
    parlay_id bigint NOT NULL REFERENCES model.parlay_tickets(parlay_id) ON DELETE CASCADE,
    fixture_id bigint REFERENCES core.fixtures(fixture_id) ON DELETE SET NULL,
    market_code text NOT NULL,
    selection_code text NOT NULL,
    taken_odd numeric(10, 4),
    label text,
    leg_result text,
    CHECK (leg_result IS NULL OR leg_result IN ('WON', 'LOST', 'VOID'))
);

CREATE INDEX IF NOT EXISTS idx_parlay_legs_parlay ON model.parlay_legs (parlay_id);
CREATE INDEX IF NOT EXISTS idx_parlay_tickets_pending
    ON model.parlay_tickets (result_code) WHERE result_code IS NULL;

-- Vue pour le Back : un combine = une ligne, avec le detail des jambes.
CREATE OR REPLACE VIEW reporting.v_parlay_board AS
SELECT
    pt.parlay_id,
    pt.combined_odd,
    pt.stake_amount,
    pt.leg_count,
    pt.label,
    pt.note,
    pt.taken_at,
    pt.result_code,
    pt.profit_units,
    pt.settled_at,
    MIN(f.kickoff_utc) AS first_kickoff,
    MAX(f.kickoff_utc) AS last_kickoff,
    string_agg(
        pl.label || ' (' || pl.selection_code || ')' ||
        CASE pl.leg_result WHEN 'WON' THEN ' OK' WHEN 'LOST' THEN ' KO' ELSE '' END,
        ' + ' ORDER BY pl.leg_id
    ) AS legs_summary
FROM model.parlay_tickets pt
LEFT JOIN model.parlay_legs pl ON pl.parlay_id = pt.parlay_id
LEFT JOIN core.fixtures f ON f.fixture_id = pl.fixture_id
GROUP BY pt.parlay_id;

GRANT SELECT, INSERT, UPDATE, DELETE ON model.parlay_tickets, model.parlay_legs TO spe_app_rw;
GRANT SELECT ON reporting.v_parlay_board TO spe_app_rw, spe_readonly;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA model TO spe_app_rw;

COMMIT;
