-- 0044 : pipeline payload brut -> normalisation.

BEGIN;

ALTER TABLE raw.provider_payloads
    ADD COLUMN IF NOT EXISTS request_path text,
    ADD COLUMN IF NOT EXISTS request_params jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS source_url text,
    ADD COLUMN IF NOT EXISTS http_status integer,
    ADD COLUMN IF NOT EXISTS payload_size_bytes integer,
    ADD COLUMN IF NOT EXISTS entity_count integer,
    ADD COLUMN IF NOT EXISTS captured_by text NOT NULL DEFAULT current_user;

CREATE INDEX IF NOT EXISTS idx_provider_payloads_captured_at
    ON raw.provider_payloads (captured_at DESC);

CREATE TABLE IF NOT EXISTS ops.provider_payload_normalizations (
    normalization_id bigserial PRIMARY KEY,
    provider_payload_id bigint NOT NULL REFERENCES raw.provider_payloads(provider_payload_id) ON DELETE CASCADE,
    ingestion_run_id uuid REFERENCES ops.ingestion_runs(ingestion_run_id) ON DELETE SET NULL,
    normalization_target text NOT NULL,
    status_code text NOT NULL DEFAULT 'PENDING',
    records_written integer NOT NULL DEFAULT 0,
    error_message text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    normalized_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status_code IN ('PENDING', 'RUNNING', 'SUCCESS', 'PARTIAL_SUCCESS', 'FAILED', 'SKIPPED')),
    CHECK (records_written >= 0),
    UNIQUE (provider_payload_id, normalization_target)
);

CREATE INDEX IF NOT EXISTS idx_payload_normalizations_payload
    ON ops.provider_payload_normalizations (provider_payload_id, normalized_at DESC);

CREATE INDEX IF NOT EXISTS idx_payload_normalizations_status
    ON ops.provider_payload_normalizations (status_code, normalized_at DESC);

CREATE OR REPLACE VIEW reporting.v_provider_payload_pipeline AS
SELECT
    rp.provider_payload_id,
    p.provider_code,
    p.provider_name,
    pe.endpoint_code,
    ir.run_scope,
    ir.application_name,
    ir.trigger_source,
    rp.provider_object_type,
    rp.provider_object_id,
    rp.natural_key,
    rp.request_path,
    rp.request_params,
    rp.source_url,
    rp.http_status,
    rp.payload_size_bytes,
    rp.entity_count,
    rp.payload_checksum,
    rp.captured_at,
    rp.captured_by,
    latest.normalization_target,
    latest.status_code AS normalization_status,
    latest.records_written,
    latest.error_message,
    latest.metadata AS normalization_metadata,
    latest.normalized_at,
    rp.ingestion_run_id
FROM raw.provider_payloads rp
JOIN ops.providers p ON p.provider_id = rp.provider_id
LEFT JOIN ops.provider_endpoints pe ON pe.endpoint_id = rp.endpoint_id
LEFT JOIN ops.ingestion_runs ir ON ir.ingestion_run_id = rp.ingestion_run_id
LEFT JOIN LATERAL (
    SELECT
        n.normalization_target,
        n.status_code,
        n.records_written,
        n.error_message,
        n.metadata,
        n.normalized_at
    FROM ops.provider_payload_normalizations n
    WHERE n.provider_payload_id = rp.provider_payload_id
    ORDER BY n.normalized_at DESC, n.normalization_id DESC
    LIMIT 1
) latest ON true;

GRANT SELECT, INSERT, UPDATE, DELETE ON ops.provider_payload_normalizations TO spe_app_rw;
GRANT SELECT ON ops.provider_payload_normalizations TO spe_readonly;
GRANT SELECT ON reporting.v_provider_payload_pipeline TO spe_app_rw, spe_readonly;
GRANT USAGE, SELECT ON SEQUENCE ops.provider_payload_normalizations_normalization_id_seq TO spe_app_rw;

COMMIT;
