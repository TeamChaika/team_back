-- Full, unfiltered accounting-time reports; immutable observation lines and current pointers.
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(7623011091001);
ALTER TABLE chaika.sync_runs DROP CONSTRAINT sync_runs_job_check;
ALTER TABLE chaika.sync_runs ADD CONSTRAINT sync_runs_job_check
    CHECK (job IN ('references', 'inventory', 'employees', 'store_balances', 'dictionaries', 'counteragent_balances'));
ALTER TABLE chaika.raw_snapshots DROP CONSTRAINT raw_snapshots_resource_check;
ALTER TABLE chaika.raw_snapshots ADD CONSTRAINT raw_snapshots_resource_check CHECK (resource IN
    ('server_type','departments','groups','stores','replication','products','product_groups',
     'incoming_invoices','writeoffs','assembly_charts','employees','store_balances','counteragents','measure_units',
     'product_categories','counteragent_balances'));

CREATE TABLE chaika.counteragent_balance_reports (
    source_id text NOT NULL REFERENCES chaika.sources(id),
    accounting_timestamp timestamp without time zone NOT NULL,
    last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    row_count integer NOT NULL CHECK (row_count >= 0),
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    PRIMARY KEY (source_id, accounting_timestamp),
    CHECK (accounting_timestamp = date_trunc('second', accounting_timestamp))
);
CREATE INDEX counteragent_balance_reports_snapshot_idx ON chaika.counteragent_balance_reports(last_snapshot_id);

CREATE TABLE chaika.counteragent_balance_items (
    snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    line_num integer NOT NULL CHECK (line_num > 0),
    account_id uuid NOT NULL,
    counteragent_id uuid,
    department_id uuid,
    sum numeric NOT NULL CHECK (sum NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)),
    PRIMARY KEY (snapshot_id, line_num)
);
CREATE INDEX counteragent_balance_items_dimensions_idx
    ON chaika.counteragent_balance_items(account_id, counteragent_id, department_id);
COMMENT ON TABLE chaika.counteragent_balance_reports IS
    'Latest complete unfiltered report per source and accounting timestamp. Join items by last_snapshot_id.';
COMMENT ON COLUMN chaika.counteragent_balance_reports.accounting_timestamp IS
    'Accounting time sent to iiko verbatim, without UTC conversion.';
COMMENT ON COLUMN chaika.counteragent_balance_items.line_num IS
    '1-based row position in the original array; preserves repeated account/counteragent/department combinations.';
-- Preserve null dimensions and unknown UUIDs: dictionary coverage cannot discard valid balances.
COMMENT ON COLUMN chaika.counteragent_balance_items.sum IS
    'Exact signed source balance; do not aggregate across accounts or infer debt direction without account semantics.';
REVOKE ALL ON chaika.counteragent_balance_reports, chaika.counteragent_balance_items FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON chaika.counteragent_balance_reports TO chaika_backend;
GRANT SELECT, INSERT ON chaika.counteragent_balance_items TO chaika_backend;
ALTER TABLE chaika.counteragent_balance_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.counteragent_balance_items ENABLE ROW LEVEL SECURITY;
CREATE POLICY backend_access ON chaika.counteragent_balance_reports TO chaika_backend USING(true) WITH CHECK(true);
CREATE POLICY backend_read ON chaika.counteragent_balance_items FOR SELECT TO chaika_backend USING(true);
CREATE POLICY backend_append ON chaika.counteragent_balance_items FOR INSERT TO chaika_backend WITH CHECK(true);
