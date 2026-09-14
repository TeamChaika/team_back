SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(7623011091001);
ALTER TABLE chaika.sync_runs DROP CONSTRAINT sync_runs_job_check;
ALTER TABLE chaika.sync_runs ADD CONSTRAINT sync_runs_job_check
    CHECK (job IN ('references','inventory','employees','store_balances','dictionaries'));
ALTER TABLE chaika.raw_snapshots DROP CONSTRAINT raw_snapshots_resource_check;
ALTER TABLE chaika.raw_snapshots ADD CONSTRAINT raw_snapshots_resource_check CHECK (resource IN
    ('server_type','departments','groups','stores','replication','products','product_groups',
     'incoming_invoices','writeoffs','assembly_charts','employees','store_balances',
     'counteragents','measure_units','product_categories'));

CREATE TABLE chaika.counteragents (
    source_id text NOT NULL REFERENCES chaika.sources(id), id uuid NOT NULL,
    code text NOT NULL, name text NOT NULL,
    deleted boolean, supplier boolean, employee boolean, client boolean,
    represents_store boolean, represented_store_id uuid,
    present_in_latest boolean NOT NULL DEFAULT true,
    first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
    last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id), details jsonb NOT NULL,
    PRIMARY KEY(source_id,id)
);
CREATE INDEX counteragents_code_idx ON chaika.counteragents(source_id,code);
CREATE INDEX counteragents_store_idx ON chaika.counteragents(source_id,represented_store_id);
CREATE INDEX counteragents_snapshot_idx ON chaika.counteragents(last_snapshot_id);
COMMENT ON TABLE chaika.counteragents IS 'Records returned by /suppliers; not an exhaustive customer directory.';

CREATE TABLE chaika.measure_units (
    source_id text NOT NULL REFERENCES chaika.sources(id), id uuid NOT NULL,
    root_type text NOT NULL CHECK(root_type='MeasureUnit'), code text, name text NOT NULL,
    deleted boolean NOT NULL, present_in_latest boolean NOT NULL DEFAULT true,
    first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
    last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id), details jsonb NOT NULL,
    PRIMARY KEY(source_id,id)
);
CREATE INDEX measure_units_snapshot_idx ON chaika.measure_units(last_snapshot_id);
CREATE TABLE chaika.product_categories (
    source_id text NOT NULL REFERENCES chaika.sources(id), id uuid NOT NULL,
    root_type text CHECK(root_type='ProductCategory'), code text, name text NOT NULL,
    deleted boolean NOT NULL, present_in_latest boolean NOT NULL DEFAULT true,
    first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
    last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id), details jsonb NOT NULL,
    PRIMARY KEY(source_id,id)
);
CREATE INDEX product_categories_snapshot_idx ON chaika.product_categories(last_snapshot_id);
-- Original flags and missing values are retained; missing reference UUIDs are reported, not dropped.
DO $policies$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['counteragents','measure_units','product_categories'] LOOP
        EXECUTE format('REVOKE ALL ON chaika.%I FROM PUBLIC, anon, authenticated',t);
        EXECUTE format('GRANT SELECT,INSERT,UPDATE ON chaika.%I TO chaika_backend',t);
        EXECUTE format('ALTER TABLE chaika.%I ENABLE ROW LEVEL SECURITY',t);
        EXECUTE format('CREATE POLICY backend_access ON chaika.%I TO chaika_backend USING(true) WITH CHECK(true)',t);
    END LOOP;
END
$policies$;
