-- Apply before starting the updated portal and document worker.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
SET LOCAL search_path=chaika_iiko_documents,pg_catalog;
ALTER TABLE waybills ADD COLUMN IF NOT EXISTS receipt_state text NOT NULL DEFAULT 'none'
    CHECK (receipt_state IN ('none','pending_sender','accepted','rejected'));
ALTER TABLE waybills_items ADD COLUMN IF NOT EXISTS received_amount double precision
    CHECK (received_amount >= 0 AND received_amount <= 1e9);
COMMIT;
