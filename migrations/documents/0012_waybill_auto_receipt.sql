-- Apply as documents migrator before starting the scheduler. Activation is separate.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
SET LOCAL search_path=chaika_iiko_documents,pg_catalog;

CREATE TABLE waybill_auto_receipt_policy (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    enabled_at timestamptz
);
INSERT INTO waybill_auto_receipt_policy (singleton,enabled_at) VALUES (true,NULL);
GRANT SELECT ON waybill_auto_receipt_policy TO chaika_iiko_app;

-- The system has no staff account, credentials or warehouse grants.
-- Keep the existing actor FKs and require an employee for every manual operation.
ALTER TABLE portal_documents_operation
    ALTER COLUMN actor_id DROP NOT NULL,
    ADD CONSTRAINT portal_documents_operation_actor_check CHECK (
        (actor_id IS NULL) = (kind='waybill' AND action='auto_receive')
    );
ALTER TABLE portal_documents_event
    ALTER COLUMN actor_id DROP NOT NULL,
    ADD CONSTRAINT portal_documents_event_actor_check CHECK (
        (actor_id IS NOT NULL AND action<>'auto_receive') OR
        (actor_id IS NULL AND kind='waybill' AND action IN (
            'auto_receive','iiko_sent','iiko_unknown','iiko_rejected',
            'reconciled_absent','reconciled_sent'
        ))
    );
COMMIT;
