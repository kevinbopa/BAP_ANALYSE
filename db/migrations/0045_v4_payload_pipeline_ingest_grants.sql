-- 0045 : grants payload pipeline pour le role d'ingestion.

BEGIN;

GRANT SELECT, INSERT, UPDATE, DELETE
ON ops.provider_payload_normalizations
TO spe_ingest_rw;

GRANT USAGE, SELECT
ON SEQUENCE ops.provider_payload_normalizations_normalization_id_seq
TO spe_ingest_rw;

COMMIT;
