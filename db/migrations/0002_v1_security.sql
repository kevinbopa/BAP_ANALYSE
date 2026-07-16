BEGIN;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'spe_owner') THEN
        CREATE ROLE spe_owner NOLOGIN;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'spe_ingest_rw') THEN
        CREATE ROLE spe_ingest_rw NOINHERIT LOGIN;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'spe_app_rw') THEN
        CREATE ROLE spe_app_rw NOINHERIT LOGIN;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'spe_readonly') THEN
        CREATE ROLE spe_readonly NOINHERIT LOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA ops TO spe_ingest_rw, spe_app_rw, spe_readonly;
GRANT USAGE ON SCHEMA raw TO spe_ingest_rw;
GRANT USAGE ON SCHEMA core TO spe_ingest_rw, spe_app_rw, spe_readonly;
GRANT USAGE ON SCHEMA model TO spe_app_rw, spe_readonly;
GRANT USAGE ON SCHEMA reporting TO spe_app_rw, spe_readonly;

GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA ops TO spe_ingest_rw;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA raw TO spe_ingest_rw;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA core TO spe_ingest_rw;

GRANT SELECT ON ALL TABLES IN SCHEMA ops TO spe_app_rw, spe_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA core TO spe_app_rw, spe_readonly;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA model TO spe_app_rw;
GRANT SELECT ON ALL TABLES IN SCHEMA model TO spe_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO spe_app_rw, spe_readonly;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ops TO spe_ingest_rw, spe_app_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA raw TO spe_ingest_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA core TO spe_ingest_rw, spe_app_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA model TO spe_app_rw;

ALTER DEFAULT PRIVILEGES IN SCHEMA ops
    GRANT SELECT, INSERT, UPDATE ON TABLES TO spe_ingest_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA raw
    GRANT SELECT, INSERT ON TABLES TO spe_ingest_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA core
    GRANT SELECT, INSERT, UPDATE ON TABLES TO spe_ingest_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA core
    GRANT SELECT ON TABLES TO spe_app_rw, spe_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA model
    GRANT SELECT, INSERT, UPDATE ON TABLES TO spe_app_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA model
    GRANT SELECT ON TABLES TO spe_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA reporting
    GRANT SELECT ON TABLES TO spe_app_rw, spe_readonly;

COMMENT ON ROLE spe_owner IS 'Role proprietaire des objets PostgreSQL du projet Sports Prediction Engine';
COMMENT ON ROLE spe_ingest_rw IS 'Role applicatif dedie aux pipelines d ingestion TheSportsDB';
COMMENT ON ROLE spe_app_rw IS 'Role applicatif dedie au backend FastAPI et aux ecritures modeles';
COMMENT ON ROLE spe_readonly IS 'Role lecture seule dedie au dashboard et au reporting';

COMMIT;
