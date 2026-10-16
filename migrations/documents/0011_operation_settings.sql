-- Apply after analytics 20261016123000_operation_settings.sql.
-- Document operations only read the company selection; they cannot change it.
BEGIN;
SET LOCAL lock_timeout='5s';
GRANT USAGE ON SCHEMA chaika TO chaika_iiko_app;
GRANT SELECT ON chaika.operation_settings TO chaika_iiko_app;
CREATE POLICY documents_read ON chaika.operation_settings FOR SELECT
    TO chaika_iiko_app USING(true);
COMMIT;
