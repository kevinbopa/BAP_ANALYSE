-- 0027 : autoriser spe_ingest_rw a purger le log de blessures d'une equipe.
--
-- L'ingestion API-Football (premium) re-ingere tout le log de blessures de la
-- saison a chaque run. Pour rester idempotent SANS accumuler des doublons, le
-- runner fait DELETE (equipe, saison) puis reinsert. Le role ingestion doit
-- donc pouvoir DELETE sur cette table de faits qu'il alimente.

BEGIN;

GRANT DELETE ON core.player_injuries TO spe_ingest_rw;

COMMIT;
