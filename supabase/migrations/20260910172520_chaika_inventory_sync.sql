-- Current entities plus immutable source observations; no passwords or source data.
-- Forward-only once data has been loaded.
ALTER TABLE chaika.sync_runs DROP CONSTRAINT sync_runs_job_check;
ALTER TABLE chaika.sync_runs ADD CONSTRAINT sync_runs_job_check CHECK (job IN ('references','inventory'));
ALTER TABLE chaika.raw_snapshots DROP CONSTRAINT raw_snapshots_resource_check;
ALTER TABLE chaika.raw_snapshots ADD CONSTRAINT raw_snapshots_resource_check CHECK (resource IN
    ('server_type','departments','groups','stores','replication','products','product_groups',
     'incoming_invoices','writeoffs','assembly_charts'));

CREATE TABLE chaika.product_groups (
    source_id text NOT NULL REFERENCES chaika.sources(id), id uuid NOT NULL,
    name text NOT NULL, parent_id uuid, code text, num text, deleted boolean NOT NULL,
    details jsonb NOT NULL, present_in_latest boolean NOT NULL DEFAULT true,
    first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
    last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    PRIMARY KEY(source_id,id)
);
CREATE INDEX product_groups_parent_idx ON chaika.product_groups(source_id,parent_id);
CREATE INDEX product_groups_snapshot_idx ON chaika.product_groups(last_snapshot_id);

CREATE TABLE chaika.products (
    source_id text NOT NULL REFERENCES chaika.sources(id), id uuid NOT NULL,
    name text NOT NULL, type text NOT NULL, group_id uuid, main_unit_id uuid NOT NULL,
    category_id uuid, code text, num text, deleted boolean NOT NULL,
    default_sale_price numeric NOT NULL, unit_weight numeric NOT NULL, unit_capacity numeric NOT NULL,
    details jsonb NOT NULL, present_in_latest boolean NOT NULL DEFAULT true,
    first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
    last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    PRIMARY KEY(source_id,id)
);
CREATE INDEX products_group_idx ON chaika.products(source_id,group_id);
CREATE INDEX products_snapshot_idx ON chaika.products(last_snapshot_id);

CREATE TABLE chaika.incoming_invoices (
    source_id text NOT NULL REFERENCES chaika.sources(id), id uuid NOT NULL,
    document_number text, date_incoming text, incoming_date text, status text,
    supplier_id uuid, default_store_id uuid, revision bigint,
    last_export_date date NOT NULL, details jsonb NOT NULL,
    first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
    last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    PRIMARY KEY(source_id,id)
);
CREATE INDEX incoming_invoices_export_idx ON chaika.incoming_invoices(source_id,last_export_date);
CREATE INDEX incoming_invoices_snapshot_idx ON chaika.incoming_invoices(last_snapshot_id);
CREATE TABLE chaika.incoming_invoice_items (
    source_id text NOT NULL, document_id uuid NOT NULL, num integer NOT NULL,
    product_id uuid, store_id uuid, amount numeric, actual_amount numeric, price numeric,
    sum numeric NOT NULL, amount_unit_id uuid, details jsonb NOT NULL,
    present_in_latest boolean NOT NULL DEFAULT true,
    PRIMARY KEY(source_id,document_id,num),
    FOREIGN KEY(source_id,document_id) REFERENCES chaika.incoming_invoices(source_id,id)
);
CREATE INDEX incoming_items_product_idx ON chaika.incoming_invoice_items(source_id,product_id);

CREATE TABLE chaika.writeoffs (
    source_id text NOT NULL REFERENCES chaika.sources(id), id uuid NOT NULL,
    document_number text NOT NULL, date_incoming timestamp without time zone NOT NULL,
    status text NOT NULL, store_id uuid NOT NULL, account_id uuid NOT NULL,
    last_export_date date NOT NULL, details jsonb NOT NULL,
    first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
    last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    PRIMARY KEY(source_id,id)
);
CREATE INDEX writeoffs_date_idx ON chaika.writeoffs(source_id,date_incoming);
CREATE INDEX writeoffs_snapshot_idx ON chaika.writeoffs(last_snapshot_id);
CREATE TABLE chaika.writeoff_items (
    source_id text NOT NULL, document_id uuid NOT NULL, num integer NOT NULL,
    product_id uuid NOT NULL, amount numeric NOT NULL, cost numeric,
    measure_unit_id uuid, amount_factor numeric, details jsonb NOT NULL,
    present_in_latest boolean NOT NULL DEFAULT true,
    PRIMARY KEY(source_id,document_id,num),
    FOREIGN KEY(source_id,document_id) REFERENCES chaika.writeoffs(source_id,id)
);
CREATE INDEX writeoff_items_product_idx ON chaika.writeoff_items(source_id,product_id);

CREATE TABLE chaika.assembly_charts (
    source_id text NOT NULL REFERENCES chaika.sources(id), id uuid NOT NULL,
    product_id uuid NOT NULL, date_from date NOT NULL, date_to date,
    assembled_amount numeric NOT NULL, details jsonb NOT NULL,
    first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
    last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    CHECK(date_to IS NULL OR date_to > date_from), PRIMARY KEY(source_id,id)
);
CREATE INDEX assembly_charts_product_idx ON chaika.assembly_charts(source_id,product_id,date_from);
CREATE INDEX assembly_charts_snapshot_idx ON chaika.assembly_charts(last_snapshot_id);
CREATE TABLE chaika.assembly_chart_items (
    source_id text NOT NULL, chart_id uuid NOT NULL, id uuid NOT NULL,
    product_id uuid NOT NULL, amount_in numeric NOT NULL, amount_middle numeric NOT NULL,
    amount_out numeric NOT NULL, sort_weight numeric NOT NULL, details jsonb NOT NULL,
    present_in_latest boolean NOT NULL DEFAULT true,
    PRIMARY KEY(source_id,chart_id,id),
    FOREIGN KEY(source_id,chart_id) REFERENCES chaika.assembly_charts(source_id,id)
);
CREATE INDEX assembly_items_product_idx ON chaika.assembly_chart_items(source_id,product_id);
CREATE TABLE chaika.assembly_chart_scopes (
    source_id text NOT NULL, business_date date NOT NULL, chart_id uuid NOT NULL,
    present_in_latest boolean NOT NULL DEFAULT true,
    last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    PRIMARY KEY(source_id,business_date,chart_id),
    FOREIGN KEY(source_id,chart_id) REFERENCES chaika.assembly_charts(source_id,id)
);
CREATE INDEX assembly_scopes_chart_idx ON chaika.assembly_chart_scopes(source_id,chart_id);
CREATE INDEX assembly_scopes_snapshot_idx ON chaika.assembly_chart_scopes(last_snapshot_id);

-- Links to products/stores may refer to deleted entries absent from active catalog exports.
-- Keep the original UUID and report unresolved links instead of inventing reference rows.
DO $policies$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['product_groups','products','incoming_invoices','incoming_invoice_items',
        'writeoffs','writeoff_items','assembly_charts','assembly_chart_items','assembly_chart_scopes']
    LOOP
        EXECUTE format('REVOKE ALL ON chaika.%I FROM PUBLIC, anon, authenticated', t);
        EXECUTE format('GRANT SELECT,INSERT,UPDATE ON chaika.%I TO chaika_backend', t);
        EXECUTE format('ALTER TABLE chaika.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY backend_access ON chaika.%I TO chaika_backend USING(true) WITH CHECK(true)', t);
    END LOOP;
END
$policies$;
