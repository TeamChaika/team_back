-- Apply after supabase/20261007120000_warehouse_access.sql, before the new API
-- and Telegram worker. No document grants or user links are rewritten.
BEGIN;
SET LOCAL lock_timeout='5s';
GRANT USAGE ON SCHEMA chaika TO chaika_iiko_app;
GRANT SELECT ON chaika.portal_warehouse_access TO chaika_iiko_app;
COMMIT;
