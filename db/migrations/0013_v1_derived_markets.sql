-- 0013 : marches derives (over/under 2.5, BTTS).
--
-- La matrice Dixon-Coles produit deja la distribution complete des scores :
-- P(over 2.5) et P(BTTS) en decoulent sans nouveau modele. Ce qui manque,
-- c'est la place en base : cotes totals/btts de The Odds API, et le droit
-- pour model.value_bets de porter un marche autre que 1X2.

BEGIN;

-- 1. model.value_bets : ouvrir les CHECK aux nouveaux marches/selections.
--    Les contraintes du baseline sont anonymes (noms auto) : on les retrouve
--    par leur definition pour les supprimer proprement.
DO $$
DECLARE
    constraint_name text;
BEGIN
    FOR constraint_name IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'model.value_bets'::regclass
          AND contype = 'c'
          AND (
              pg_get_constraintdef(oid) LIKE '%selection_code%'
              OR (
                  pg_get_constraintdef(oid) LIKE '%market_code%'
                  AND pg_get_constraintdef(oid) NOT LIKE '%probability%'
              )
          )
    LOOP
        EXECUTE format('ALTER TABLE model.value_bets DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END $$;

ALTER TABLE model.value_bets
    ADD CONSTRAINT value_bets_selection_code_chk
    CHECK (selection_code IN (
        'HOME', 'DRAW', 'AWAY', 'OVER', 'UNDER', 'BTTS_YES', 'BTTS_NO',
        'HOME_DRAW', 'HOME_AWAY', 'DRAW_AWAY'
    ));

ALTER TABLE model.value_bets
    ADD CONSTRAINT value_bets_market_code_chk
    CHECK (market_code IN (
        '1X2', 'OU15', 'OU25', 'OU35', 'BTTS',
        'DOUBLE_CHANCE', 'DNB', 'HANDICAP'
    ));

-- Coherence selection <-> marche : un OVER ne peut pas etre un pari 1X2.
ALTER TABLE model.value_bets
    ADD CONSTRAINT value_bets_market_selection_chk
    CHECK (
        (market_code = '1X2' AND selection_code IN ('HOME', 'DRAW', 'AWAY'))
        OR (market_code IN ('OU15', 'OU25', 'OU35') AND selection_code IN ('OVER', 'UNDER'))
        OR (market_code = 'BTTS' AND selection_code IN ('BTTS_YES', 'BTTS_NO'))
        OR (market_code = 'DOUBLE_CHANCE' AND selection_code IN ('HOME_DRAW', 'HOME_AWAY', 'DRAW_AWAY'))
        OR (market_code IN ('DNB', 'HANDICAP') AND selection_code IN ('HOME', 'AWAY'))
    );

-- 2. Cotes over/under par ligne (2.5 principalement, mais on garde toutes
--    les lignes recues : 1.5/3.5 serviront aux futurs marches).
CREATE TABLE IF NOT EXISTS core.fixture_odds_totals (
    fixture_odds_totals_id bigserial PRIMARY KEY,
    fixture_id bigint NOT NULL REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    bookmaker_id bigint NOT NULL REFERENCES core.bookmakers(bookmaker_id) ON DELETE CASCADE,
    captured_at timestamptz NOT NULL,
    total_line numeric(4, 2) NOT NULL,
    over_odd numeric(10, 4) NOT NULL,
    under_odd numeric(10, 4) NOT NULL,
    is_closing_line boolean NOT NULL DEFAULT false,
    source_system text NOT NULL DEFAULT 'THEODDSAPI_V4',
    source_reference text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (fixture_id, bookmaker_id, total_line, captured_at),
    CHECK (total_line > 0),
    CHECK (over_odd > 1.0000),
    CHECK (under_odd > 1.0000)
);

CREATE INDEX IF NOT EXISTS idx_fixture_odds_totals_fixture_line
    ON core.fixture_odds_totals (fixture_id, total_line, captured_at DESC);

-- 3. Cotes both-teams-to-score.
CREATE TABLE IF NOT EXISTS core.fixture_odds_btts (
    fixture_odds_btts_id bigserial PRIMARY KEY,
    fixture_id bigint NOT NULL REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    bookmaker_id bigint NOT NULL REFERENCES core.bookmakers(bookmaker_id) ON DELETE CASCADE,
    captured_at timestamptz NOT NULL,
    yes_odd numeric(10, 4) NOT NULL,
    no_odd numeric(10, 4) NOT NULL,
    is_closing_line boolean NOT NULL DEFAULT false,
    source_system text NOT NULL DEFAULT 'THEODDSAPI_V4',
    source_reference text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (fixture_id, bookmaker_id, captured_at),
    CHECK (yes_odd > 1.0000),
    CHECK (no_odd > 1.0000)
);

CREATE INDEX IF NOT EXISTS idx_fixture_odds_btts_fixture
    ON core.fixture_odds_btts (fixture_id, captured_at DESC);

-- 4. Vues "derniere cote" alignees sur reporting.v_latest_odds_1x2.
CREATE OR REPLACE VIEW reporting.v_latest_odds_totals AS
WITH ranked_odds AS (
    SELECT
        fo.*,
        row_number() OVER (
            PARTITION BY fo.fixture_id, fo.bookmaker_id, fo.total_line
            ORDER BY fo.captured_at DESC, fo.fixture_odds_totals_id DESC
        ) AS row_num
    FROM core.fixture_odds_totals fo
)
SELECT
    fixture_odds_totals_id,
    fixture_id,
    bookmaker_id,
    captured_at,
    total_line,
    over_odd,
    under_odd,
    is_closing_line,
    source_system,
    source_reference
FROM ranked_odds
WHERE row_num = 1;

CREATE OR REPLACE VIEW reporting.v_latest_odds_btts AS
WITH ranked_odds AS (
    SELECT
        fo.*,
        row_number() OVER (
            PARTITION BY fo.fixture_id, fo.bookmaker_id
            ORDER BY fo.captured_at DESC, fo.fixture_odds_btts_id DESC
        ) AS row_num
    FROM core.fixture_odds_btts fo
)
SELECT
    fixture_odds_btts_id,
    fixture_id,
    bookmaker_id,
    captured_at,
    yes_odd,
    no_odd,
    is_closing_line,
    source_system,
    source_reference
FROM ranked_odds
WHERE row_num = 1;

-- 5. Droits : l'ingestion ecrit les cotes, l'app et le readonly lisent.
GRANT SELECT, INSERT, UPDATE ON core.fixture_odds_totals, core.fixture_odds_btts TO spe_ingest_rw;
GRANT SELECT ON core.fixture_odds_totals, core.fixture_odds_btts TO spe_app_rw, spe_readonly;
GRANT SELECT ON reporting.v_latest_odds_totals, reporting.v_latest_odds_btts TO spe_app_rw, spe_ingest_rw, spe_readonly;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA core TO spe_ingest_rw, spe_app_rw;

COMMIT;
