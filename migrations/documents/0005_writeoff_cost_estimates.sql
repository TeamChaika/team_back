-- Apply before deploying the updated portal and worker; no historical backfill.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
ALTER TABLE chaika_iiko_documents.writeoffs ADD COLUMN IF NOT EXISTS cost_estimate jsonb;
-- Standalone document test databases may not contain the analytics schema.
DO $$
BEGIN
    IF to_regclass('chaika.store_balance_reports') IS NOT NULL
       AND to_regclass('chaika.store_balance_items') IS NOT NULL THEN
        GRANT USAGE ON SCHEMA chaika TO chaika_iiko_app;
        GRANT SELECT ON chaika.store_balance_reports,chaika.store_balance_items TO chaika_iiko_app;
        DROP POLICY IF EXISTS documents_cost_reports ON chaika.store_balance_reports;
        CREATE POLICY documents_cost_reports ON chaika.store_balance_reports
            FOR SELECT TO chaika_iiko_app USING (source_id='primary');
        DROP POLICY IF EXISTS documents_cost_items ON chaika.store_balance_items;
        CREATE POLICY documents_cost_items ON chaika.store_balance_items
            FOR SELECT TO chaika_iiko_app USING (EXISTS (
                SELECT 1 FROM chaika.store_balance_reports r
                WHERE r.source_id='primary' AND r.last_snapshot_id=snapshot_id
            ));
    END IF;
END $$;
COMMIT;
