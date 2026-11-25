BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
SET LOCAL search_path=chaika_iiko_documents,pg_catalog;

CREATE TABLE waybill_auto_receipt_reminders (
    document_id bigint NOT NULL REFERENCES waybills(id) ON DELETE CASCADE,
    recipient_id bigint NOT NULL REFERENCES authentication_user(id) ON DELETE CASCADE,
    claim_id uuid NOT NULL,
    state text NOT NULL CHECK (state IN ('reserved','sending','sent','unknown','retry','rejected')),
    updated_at timestamptz NOT NULL DEFAULT now(),
    retry_at timestamptz,
    PRIMARY KEY (document_id,recipient_id)
);
CREATE INDEX waybill_auto_receipt_reminders_inflight ON waybill_auto_receipt_reminders(updated_at)
    WHERE state='sending';
GRANT SELECT,INSERT,UPDATE,DELETE ON waybill_auto_receipt_reminders TO chaika_iiko_app;
COMMIT;
