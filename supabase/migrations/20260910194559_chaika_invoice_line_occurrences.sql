-- iiko historical XML can repeat a line number within one invoice.
-- Keep the source number and distinguish its occurrences in source order.
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(7623011091001);
ALTER TABLE chaika.incoming_invoice_items
    ADD COLUMN num_occurrence integer NOT NULL DEFAULT 1 CHECK (num_occurrence > 0);
ALTER TABLE chaika.incoming_invoice_items DROP CONSTRAINT incoming_invoice_items_pkey;
ALTER TABLE chaika.incoming_invoice_items ADD PRIMARY KEY
    (source_id, document_id, num, num_occurrence);
COMMENT ON COLUMN chaika.incoming_invoice_items.num_occurrence IS
    '1-based occurrence of the source num in XML order; not an iiko identifier.';
