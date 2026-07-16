-- 0012 - Contexte evenement depuis la fiche complete TheSportsDB
-- (lookupevent.php, schema 49 champs vs 30 pour eventsseason.php).
--
-- Capture : affluence (intSpectators -> facteur CROWD), meteo constatee
-- (strWeather -> facteur WEATHER historique), ville/pays du stade
-- (-> distance de deplacement + coordonnees pour la meteo PREVISIONNELLE
-- des matchs futurs via Open-Meteo).
--
-- Couverture assumee : riche sur les grands evenements recents, maigre sur
-- les vieux matchs mineurs — les colonnes restent NULL quand la fiche est vide.

BEGIN;

ALTER TABLE core.fixtures
    ADD COLUMN IF NOT EXISTS attendance integer,
    ADD COLUMN IF NOT EXISTS weather_text text,
    ADD COLUMN IF NOT EXISTS venue_city text,
    ADD COLUMN IF NOT EXISTS venue_country text;

-- Progression du backfill contexte (reprenable, comme le backfill joueurs).
CREATE TABLE IF NOT EXISTS ops.event_context_progress (
    fixture_id bigint PRIMARY KEY REFERENCES core.fixtures(fixture_id) ON DELETE CASCADE,
    status_code text NOT NULL DEFAULT 'DONE'
        CHECK (status_code IN ('DONE', 'NO_DATA', 'ERROR')),
    fields_filled integer NOT NULL DEFAULT 0,
    error_message text,
    processed_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON ops.event_context_progress TO spe_ingest_rw;
GRANT SELECT ON ops.event_context_progress TO spe_app_rw, spe_readonly;
GRANT SELECT, INSERT, UPDATE ON core.fixtures TO spe_ingest_rw;

COMMIT;
