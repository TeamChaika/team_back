SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(7623011091001);
ALTER TABLE chaika.raw_snapshots DROP CONSTRAINT raw_snapshots_resource_check;
ALTER TABLE chaika.raw_snapshots ADD CONSTRAINT raw_snapshots_resource_check CHECK (resource IN
    ('server_type','departments','groups','stores','replication','products','product_groups',
     'incoming_invoices','writeoffs','assembly_charts','employees','store_balances','counteragents',
     'measure_units','product_categories','counteragent_balances','outgoing_invoices'));
CREATE TABLE chaika.outgoing_invoices (
    source_id text NOT NULL REFERENCES chaika.sources(id), id uuid NOT NULL,
    document_number text, date_incoming text NOT NULL,
    status text NOT NULL CHECK(status IN ('NEW','PROCESSED','DELETED')),
    counteragent_id uuid, default_store_id uuid,
    linked_incoming_invoice_id uuid, linked_outgoing_invoice_id uuid,
    last_export_date date NOT NULL, details jsonb NOT NULL,
    first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
    last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    PRIMARY KEY(source_id,id)
);
CREATE INDEX outgoing_invoices_export_idx ON chaika.outgoing_invoices(source_id,last_export_date);
CREATE INDEX outgoing_invoices_snapshot_idx ON chaika.outgoing_invoices(last_snapshot_id);
CREATE INDEX outgoing_invoices_link_idx ON chaika.outgoing_invoices(source_id,linked_incoming_invoice_id);
CREATE TABLE chaika.outgoing_invoice_items (
    source_id text NOT NULL, document_id uuid NOT NULL, line_num integer NOT NULL CHECK(line_num>0),
    product_id uuid, store_id uuid, container_id uuid,
    amount numeric, price numeric,
    sum numeric NOT NULL CHECK(sum NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)),
    details jsonb NOT NULL, present_in_latest boolean NOT NULL DEFAULT true,
    PRIMARY KEY(source_id,document_id,line_num),
    FOREIGN KEY(source_id,document_id) REFERENCES chaika.outgoing_invoices(source_id,id)
);
CREATE INDEX outgoing_items_product_idx ON chaika.outgoing_invoice_items(source_id,product_id);
COMMENT ON COLUMN chaika.outgoing_invoice_items.line_num IS
    '1-based position in the XML array, not an iiko row identifier; original observations remain in RAW.';
COMMENT ON COLUMN chaika.outgoing_invoices.linked_incoming_invoice_id IS
    'Original linkedIncomingInvoiceId from iiko. Missing pairs are reported, never inferred from document numbers.';
COMMENT ON TABLE chaika.outgoing_invoices IS
    'All returned statuses and counterparties. Do not treat drafts or unverified recipients as completed internal transfers.';
DO $policies$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['outgoing_invoices','outgoing_invoice_items'] LOOP
        EXECUTE format('REVOKE ALL ON chaika.%I FROM PUBLIC, anon, authenticated',t);
        EXECUTE format('GRANT SELECT,INSERT,UPDATE ON chaika.%I TO chaika_backend',t);
        EXECUTE format('ALTER TABLE chaika.%I ENABLE ROW LEVEL SECURITY',t);
        EXECUTE format('CREATE POLICY backend_access ON chaika.%I TO chaika_backend USING(true) WITH CHECK(true)',t);
    END LOOP;
END
$policies$;
