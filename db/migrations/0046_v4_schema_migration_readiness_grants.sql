-- 0046 : lecture du ledger de migration pour le role applicatif.

BEGIN;

GRANT USAGE ON SCHEMA public TO spe_app_rw, spe_readonly;

GRANT SELECT
ON TABLE public.schema_migrations
TO spe_app_rw, spe_readonly;

COMMIT;
