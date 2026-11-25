BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE chaika.incoming_invoices
    ADD COLUMN processed_first_seen_at timestamptz,
    ADD COLUMN paired_first_seen_at timestamptz;
ALTER TABLE chaika.outgoing_invoices
    ADD COLUMN processed_first_seen_at timestamptz,
    ADD COLUMN paired_first_seen_at timestamptz;

CREATE TABLE chaika.purchase_digest_receipt_pairs (
    source_id text NOT NULL,
    incoming_id uuid NOT NULL,
    first_paired_at timestamptz NOT NULL,
    PRIMARY KEY (source_id, incoming_id)
);
ALTER TABLE chaika.purchase_digest_receipt_pairs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON chaika.purchase_digest_receipt_pairs FROM PUBLIC, anon, authenticated;
GRANT SELECT,INSERT ON chaika.purchase_digest_receipt_pairs TO chaika_backend;
CREATE POLICY backend_access ON chaika.purchase_digest_receipt_pairs TO chaika_backend
    USING (true) WITH CHECK (true);

-- Before this migration only first_seen_at was recorded. Preserve the original window
-- of existing reciprocal pairs rather than making them newly available on next import.
INSERT INTO chaika.purchase_digest_receipt_pairs (source_id,incoming_id,first_paired_at)
SELECT i.source_id,i.id,greatest(i.first_seen_at,o.first_seen_at)
FROM chaika.outgoing_invoices o
JOIN chaika.incoming_invoices i ON (i.source_id,i.id)=(o.source_id,o.linked_incoming_invoice_id)
WHERE i.status='PROCESSED' AND o.status='PROCESSED'
  AND i.details->>'linked_outgoing_invoice_id'=o.id::text
ON CONFLICT DO NOTHING;

-- First posting is immutable, but the start of the CURRENT posted link changes on
-- correction. This also supplies a read-time fallback if two imports commit concurrently.
CREATE FUNCTION chaika.purchase_digest_mark_available() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE
    old_link text;
    new_link text;
BEGIN
    IF TG_TABLE_NAME='incoming_invoices' THEN
        new_link := nullif(NEW.details->>'linked_outgoing_invoice_id','');
        IF TG_OP='UPDATE' THEN old_link := nullif(OLD.details->>'linked_outgoing_invoice_id',''); END IF;
    ELSE
        new_link := NEW.linked_incoming_invoice_id::text;
        IF TG_OP='UPDATE' THEN old_link := OLD.linked_incoming_invoice_id::text; END IF;
    END IF;
    IF TG_OP='UPDATE' THEN
        NEW.processed_first_seen_at := coalesce(OLD.processed_first_seen_at,
            CASE WHEN OLD.status='PROCESSED' THEN OLD.first_seen_at END);
    ELSE
        NEW.processed_first_seen_at := NULL;
    END IF;
    NEW.paired_first_seen_at := NULL;
    IF NEW.status='PROCESSED' THEN
        NEW.processed_first_seen_at := coalesce(NEW.processed_first_seen_at,NEW.last_seen_at);
        IF new_link IS NOT NULL THEN
            IF TG_OP='UPDATE' AND OLD.status='PROCESSED' AND old_link=new_link THEN
                NEW.paired_first_seen_at := coalesce(OLD.paired_first_seen_at,OLD.first_seen_at);
            ELSE
                NEW.paired_first_seen_at := NEW.last_seen_at;
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION chaika.purchase_digest_mark_available() FROM PUBLIC;

-- Only successful inserts/updates may publish a pair: BEFORE INSERT also runs for
-- the discarded candidate of INSERT ... ON CONFLICT DO UPDATE. Remember an outgoing
-- correction on either side; later unpair/re-pair must never re-emit the receipt.
CREATE FUNCTION chaika.purchase_digest_remember_pair() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE
    leg record;
    incoming uuid;
    outgoing text;
    changed boolean := true;
    phase integer;
BEGIN
    IF TG_OP='UPDATE' AND NEW.status='PROCESSED' THEN
        IF TG_TABLE_NAME='incoming_invoices' THEN
            changed := (OLD.details->>'linked_outgoing_invoice_id')
                IS DISTINCT FROM (NEW.details->>'linked_outgoing_invoice_id');
        ELSE
            changed := OLD.linked_incoming_invoice_id IS DISTINCT FROM NEW.linked_incoming_invoice_id;
        END IF;
    END IF;

    FOR phase IN 1..2 LOOP
        IF phase=1 THEN
            -- Capture a formerly reciprocal pair before losing the link. This repairs
            -- the rare case where concurrent writers did not yet see each other.
            IF TG_OP='INSERT' OR NOT changed OR OLD.status IS DISTINCT FROM 'PROCESSED' THEN
                CONTINUE;
            END IF;
            leg := OLD;
        ELSE
            IF NEW.status IS DISTINCT FROM 'PROCESSED' THEN CONTINUE; END IF;
            leg := NEW;
        END IF;

        IF TG_TABLE_NAME='incoming_invoices' THEN
            incoming := leg.id;
            outgoing := nullif(leg.details->>'linked_outgoing_invoice_id','');
        ELSE
            incoming := leg.linked_incoming_invoice_id;
            outgoing := leg.id::text;
        END IF;
        IF incoming IS NULL OR outgoing IS NULL THEN CONTINUE; END IF;
        IF EXISTS (SELECT 1 FROM chaika.purchase_digest_receipt_pairs p
                   WHERE (p.source_id,p.incoming_id)=(leg.source_id,incoming)) THEN CONTINUE; END IF;

        IF TG_TABLE_NAME='incoming_invoices' THEN
            INSERT INTO chaika.purchase_digest_receipt_pairs
            SELECT leg.source_id,incoming,greatest(coalesce(leg.paired_first_seen_at,leg.first_seen_at),
                                               coalesce(o.paired_first_seen_at,o.first_seen_at))
            FROM chaika.outgoing_invoices o
            WHERE (o.source_id,o.linked_incoming_invoice_id)=(leg.source_id,incoming)
              AND o.id::text=outgoing AND o.status='PROCESSED'
            ON CONFLICT DO NOTHING;
        ELSE
            INSERT INTO chaika.purchase_digest_receipt_pairs
            SELECT leg.source_id,incoming,greatest(coalesce(leg.paired_first_seen_at,leg.first_seen_at),
                                               coalesce(i.paired_first_seen_at,i.first_seen_at))
            FROM chaika.incoming_invoices i
            WHERE (i.source_id,i.id)=(leg.source_id,incoming) AND i.status='PROCESSED'
              AND i.details->>'linked_outgoing_invoice_id'=outgoing
            ON CONFLICT DO NOTHING;
        END IF;
    END LOOP;
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION chaika.purchase_digest_remember_pair() FROM PUBLIC;
CREATE TRIGGER purchase_digest_mark_available
    BEFORE INSERT OR UPDATE OF status,details ON chaika.incoming_invoices
    FOR EACH ROW EXECUTE FUNCTION chaika.purchase_digest_mark_available();
CREATE TRIGGER purchase_digest_mark_available
    BEFORE INSERT OR UPDATE OF status,linked_incoming_invoice_id ON chaika.outgoing_invoices
    FOR EACH ROW EXECUTE FUNCTION chaika.purchase_digest_mark_available();
CREATE TRIGGER purchase_digest_remember_pair
    AFTER INSERT OR UPDATE OF status,details ON chaika.incoming_invoices
    FOR EACH ROW EXECUTE FUNCTION chaika.purchase_digest_remember_pair();
CREATE TRIGGER purchase_digest_remember_pair
    AFTER INSERT OR UPDATE OF status,linked_incoming_invoice_id ON chaika.outgoing_invoices
    FOR EACH ROW EXECUTE FUNCTION chaika.purchase_digest_remember_pair();
COMMIT;
