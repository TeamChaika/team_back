SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(7623011091001);
ALTER TABLE chaika.raw_snapshots DROP CONSTRAINT raw_snapshots_resource_check;
ALTER TABLE chaika.raw_snapshots ADD CONSTRAINT raw_snapshots_resource_check CHECK (resource IN
    ('server_type','departments','groups','stores','replication','products','product_groups',
     'incoming_invoices','writeoffs','assembly_charts','employees','store_balances','counteragents',
     'measure_units','product_categories','counteragent_balances','outgoing_invoices','transfers'));
CREATE TABLE chaika.internal_transfers (
    source_id text NOT NULL REFERENCES chaika.sources(id), id uuid NOT NULL,
    document_number text NOT NULL, date_incoming timestamp without time zone NOT NULL,
    status text NOT NULL CHECK(status IN ('NEW','PROCESSED','DELETED')),
    store_from_id uuid NOT NULL, store_to_id uuid NOT NULL,
    last_export_date date NOT NULL, details jsonb NOT NULL,
    first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
    last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    PRIMARY KEY(source_id,id)
);
CREATE INDEX internal_transfers_export_idx ON chaika.internal_transfers(source_id,last_export_date);
CREATE INDEX internal_transfers_snapshot_idx ON chaika.internal_transfers(last_snapshot_id);
CREATE INDEX internal_transfers_from_idx ON chaika.internal_transfers(source_id,store_from_id,date_incoming);
CREATE INDEX internal_transfers_to_idx ON chaika.internal_transfers(source_id,store_to_id,date_incoming);
CREATE TABLE chaika.internal_transfer_items (
    source_id text NOT NULL, document_id uuid NOT NULL, num integer NOT NULL,
    product_id uuid NOT NULL, measure_unit_id uuid, product_size_id uuid, container_id uuid,
    amount numeric NOT NULL CHECK(amount NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)),
    cost numeric CHECK(cost NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)),
    amount_factor numeric CHECK(amount_factor NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)),
    details jsonb NOT NULL, present_in_latest boolean NOT NULL DEFAULT true,
    PRIMARY KEY(source_id,document_id,num),
    FOREIGN KEY(source_id,document_id) REFERENCES chaika.internal_transfers(source_id,id)
);
CREATE INDEX internal_transfer_items_product_idx ON chaika.internal_transfer_items(source_id,product_id);
COMMENT ON TABLE chaika.internal_transfers IS
    'Original internalTransfer documents, separate from outgoing/incoming invoice pairs; all source statuses are preserved.';
COMMENT ON COLUMN chaika.internal_transfers.date_incoming IS
    'Accounting local time, without conversion to UTC. Original JSON remains in RAW.';
COMMENT ON COLUMN chaika.internal_transfer_items.cost IS
    'Original total line cost, not unit price; null is not zero.';
DO $policies$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['internal_transfers','internal_transfer_items'] LOOP
        EXECUTE format('REVOKE ALL ON chaika.%I FROM PUBLIC, anon, authenticated',t);
        EXECUTE format('GRANT SELECT,INSERT,UPDATE ON chaika.%I TO chaika_backend',t);
        EXECUTE format('ALTER TABLE chaika.%I ENABLE ROW LEVEL SECURITY',t);
        EXECUTE format('CREATE POLICY backend_access ON chaika.%I TO chaika_backend USING(true) WITH CHECK(true)',t);
    END LOOP;
END
$policies$;
