-- 0041 : observabilite des runs d'ingestion.

BEGIN;

ALTER TABLE ops.ingestion_runs
    ADD COLUMN IF NOT EXISTS application_name text,
    ADD COLUMN IF NOT EXISTS trigger_source text,
    ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz,
    ADD COLUMN IF NOT EXISTS host_name text,
    ADD COLUMN IF NOT EXISTS process_id integer,
    ADD COLUMN IF NOT EXISTS request_fingerprint text;

UPDATE ops.ingestion_runs
SET application_name = COALESCE(NULLIF(application_name, ''), created_by),
    trigger_source = COALESCE(NULLIF(trigger_source, ''), 'MANUAL'),
    heartbeat_at = COALESCE(heartbeat_at, finished_at, started_at, requested_at),
    host_name = COALESCE(NULLIF(host_name, ''), 'local'),
    request_fingerprint = COALESCE(
        NULLIF(request_fingerprint, ''),
        md5(COALESCE(request_params::text, '{}'::text))
    )
WHERE application_name IS NULL
   OR trigger_source IS NULL
   OR heartbeat_at IS NULL
   OR host_name IS NULL
   OR request_fingerprint IS NULL;

ALTER TABLE ops.ingestion_runs
    ALTER COLUMN application_name SET DEFAULT
        COALESCE(NULLIF(current_setting('application_name', true), ''), 'bp-edge'),
    ALTER COLUMN trigger_source SET DEFAULT 'MANUAL',
    ALTER COLUMN heartbeat_at SET DEFAULT now(),
    ALTER COLUMN host_name SET DEFAULT COALESCE(inet_client_addr()::text, 'local'),
    ALTER COLUMN process_id SET DEFAULT pg_backend_pid();

ALTER TABLE ops.ingestion_runs
    ALTER COLUMN trigger_source SET NOT NULL,
    ALTER COLUMN heartbeat_at SET NOT NULL,
    ALTER COLUMN host_name SET NOT NULL;

ALTER TABLE ops.ingestion_runs
    DROP CONSTRAINT IF EXISTS chk_ingestion_runs_trigger_source;
ALTER TABLE ops.ingestion_runs
    ADD CONSTRAINT chk_ingestion_runs_trigger_source
    CHECK (trigger_source IN ('MANUAL', 'SCHEDULED', 'BACKFILL', 'RECOVERY', 'UNKNOWN'));

ALTER TABLE ops.ingestion_runs
    DROP CONSTRAINT IF EXISTS chk_ingestion_runs_heartbeat_after_requested;
ALTER TABLE ops.ingestion_runs
    ADD CONSTRAINT chk_ingestion_runs_heartbeat_after_requested
    CHECK (heartbeat_at >= requested_at);

CREATE INDEX IF NOT EXISTS idx_ingestion_runs_status_heartbeat
    ON ops.ingestion_runs (status_code, heartbeat_at DESC);

CREATE INDEX IF NOT EXISTS idx_ingestion_runs_provider_scope_started
    ON ops.ingestion_runs (provider_id, run_scope, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_ingestion_runs_request_fingerprint
    ON ops.ingestion_runs (request_fingerprint, requested_at DESC);

CREATE OR REPLACE VIEW reporting.v_ingestion_run_latest AS
WITH ranked AS (
    SELECT
        ir.ingestion_run_id,
        ir.provider_id,
        p.provider_name,
        p.provider_code,
        pe.endpoint_code,
        ir.run_scope,
        ir.status_code,
        ir.requested_at,
        ir.started_at,
        ir.finished_at,
        ir.heartbeat_at,
        ir.records_received,
        ir.records_written,
        ir.error_message,
        ir.application_name,
        ir.trigger_source,
        ir.host_name,
        ir.process_id,
        ir.request_fingerprint,
        row_number() OVER (
            PARTITION BY ir.provider_id
            ORDER BY ir.requested_at DESC NULLS LAST,
                     ir.started_at DESC NULLS LAST,
                     ir.ingestion_run_id DESC
        ) AS row_num
    FROM ops.ingestion_runs ir
    JOIN ops.providers p
      ON p.provider_id = ir.provider_id
    LEFT JOIN ops.provider_endpoints pe
      ON pe.endpoint_id = ir.endpoint_id
)
SELECT
    ingestion_run_id,
    provider_id,
    provider_name,
    provider_code,
    endpoint_code,
    run_scope,
    status_code,
    requested_at,
    started_at,
    finished_at,
    heartbeat_at,
    records_received,
    records_written,
    error_message,
    application_name,
    trigger_source,
    host_name,
    process_id,
    request_fingerprint,
    GREATEST(
        0,
        EXTRACT(EPOCH FROM (COALESCE(finished_at, now()) - COALESCE(started_at, requested_at)))
    )::bigint AS elapsed_seconds,
    (
        status_code = 'RUNNING'
        AND heartbeat_at < now() - interval '2 hours'
    ) AS is_stale
FROM ranked
WHERE row_num = 1;

GRANT SELECT ON reporting.v_ingestion_run_latest TO spe_readonly, spe_app_rw;

COMMIT;
