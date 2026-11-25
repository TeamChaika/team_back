BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';
SET LOCAL search_path=chaika_iiko_documents,pg_catalog;

CREATE TABLE writeoff_reconciliation (
    document_id bigint PRIMARY KEY REFERENCES writeoffs(id) ON DELETE CASCADE,
    version integer NOT NULL,
    operation_id uuid REFERENCES native_dispatch(operation_id),
    checked_at timestamptz,
    result jsonb,
    next_check_at timestamptz NOT NULL DEFAULT now(),
    claim_id uuid
);
CREATE INDEX writeoff_reconciliation_due ON writeoff_reconciliation
    ((operation_id IS NOT NULL),next_check_at,document_id DESC);
GRANT SELECT,INSERT,UPDATE,DELETE ON writeoff_reconciliation TO chaika_iiko_app;

CREATE FUNCTION queue_writeoff_reconciliation() RETURNS trigger
LANGUAGE plpgsql SET search_path=chaika_iiko_documents,pg_catalog AS $$
DECLARE op uuid;
BEGIN
    IF NEW.status='Sent' OR NEW.submission_state='unknown' THEN
        SELECT operation_id INTO op FROM native_dispatch
        WHERE kind='writeoff' AND document_id=NEW.id AND version<=NEW.version
          AND state IN ('sending','sent','unknown')
        ORDER BY version DESC LIMIT 1;
        INSERT INTO writeoff_reconciliation(document_id,version,operation_id)
        VALUES(NEW.id,NEW.version,op) ON CONFLICT(document_id) DO UPDATE SET
          version=excluded.version,operation_id=excluded.operation_id,
          checked_at=NULL,result=NULL,claim_id=NULL,next_check_at=now()
        WHERE writeoff_reconciliation.version<>excluded.version
           OR writeoff_reconciliation.operation_id IS DISTINCT FROM excluded.operation_id;
    ELSIF TG_OP='UPDATE' AND (OLD.status='Sent' OR OLD.submission_state='unknown') THEN
        DELETE FROM writeoff_reconciliation WHERE document_id=NEW.id;
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER writeoff_reconciliation_schedule
AFTER INSERT OR UPDATE OF status,submission_state,version ON writeoffs
FOR EACH ROW EXECUTE FUNCTION queue_writeoff_reconciliation();

-- Recent confirmations first. Spread the initial legacy import over several days.
WITH eligible AS (
    SELECT w.id,w.version,d.operation_id,
        row_number() OVER (PARTITION BY d.operation_id IS NOT NULL
                           ORDER BY coalesce(d.created_at,w.created_at) DESC,w.id DESC)-1 AS n
    FROM writeoffs w LEFT JOIN LATERAL (
        SELECT operation_id,created_at FROM native_dispatch WHERE kind='writeoff'
            AND document_id=w.id AND version<=w.version AND state IN ('unknown','sent')
        ORDER BY version DESC LIMIT 1
    ) d ON true WHERE w.status='Sent' OR w.submission_state='unknown'
)
INSERT INTO writeoff_reconciliation(document_id,version,operation_id,next_check_at)
SELECT id,version,operation_id,now()+n*CASE WHEN operation_id IS NULL THEN interval '30 seconds'
                                            ELSE interval '1 second' END FROM eligible;
COMMIT;
