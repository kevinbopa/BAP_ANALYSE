-- 0028 : cotes generiques pour les marches derives supplementaires.
--
-- Les lignes O/U alternatives (0.5/1.5/3.5/4.5) reutilisent core.fixture_odds_totals
-- (colonne total_line existe deja). Pour handicap (spreads), double chance et
-- draw-no-bet, une table generique two-way : (marche, ligne, selection) -> cote.
-- team_totals ecarte : The Odds API ne l'expose pas pour le soccer.

BEGIN;

CREATE TABLE IF NOT EXISTS core.fixture_odds_market (
    fixture_odds_market_id bigserial PRIMARY KEY,
    fixture_id bigint NOT NULL REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    bookmaker_id bigint NOT NULL REFERENCES core.bookmakers(bookmaker_id) ON DELETE CASCADE,
    market_code text NOT NULL,
    -- Ligne (handicap ou total) ; 0 pour les marches sans ligne (DC, DNB).
    line numeric(5, 2) NOT NULL DEFAULT 0,
    selection_code text NOT NULL,
    decimal_odd numeric(10, 4) NOT NULL,
    captured_at timestamptz NOT NULL,
    source_system text NOT NULL DEFAULT 'THEODDSAPI_V4',
    source_reference text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (fixture_id, bookmaker_id, market_code, line, selection_code, captured_at),
    CHECK (decimal_odd > 1.0000),
    CHECK (market_code IN ('SPREAD', 'DOUBLE_CHANCE', 'DNB')),
    CHECK (selection_code IN ('HOME', 'AWAY', 'DC_1X', 'DC_12', 'DC_X2'))
);

CREATE INDEX IF NOT EXISTS idx_fixture_odds_market_fixture
    ON core.fixture_odds_market (fixture_id, market_code, captured_at DESC);

GRANT SELECT, INSERT, UPDATE ON core.fixture_odds_market TO spe_ingest_rw, spe_app_rw;
GRANT SELECT ON core.fixture_odds_market TO spe_readonly;
GRANT USAGE, SELECT ON SEQUENCE core.fixture_odds_market_fixture_odds_market_id_seq
    TO spe_ingest_rw, spe_app_rw;

COMMIT;
