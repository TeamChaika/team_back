-- Each complete day is an observation; repeated source states retain their history.
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(7623011091001);
ALTER TABLE chaika.sync_runs DROP CONSTRAINT sync_runs_job_check;
ALTER TABLE chaika.sync_runs ADD CONSTRAINT sync_runs_job_check CHECK(job IN
 ('references','inventory','employees','store_balances','dictionaries','counteragent_balances',
  'events','employee_roles','accounts','cash_shifts'));
ALTER TABLE chaika.raw_snapshots DROP CONSTRAINT raw_snapshots_resource_check;
ALTER TABLE chaika.raw_snapshots ADD CONSTRAINT raw_snapshots_resource_check CHECK(resource IN
 ('server_type','departments','groups','stores','replication','products','product_groups',
  'incoming_invoices','writeoffs','assembly_charts','employees','store_balances','counteragents',
  'measure_units','product_categories','counteragent_balances','outgoing_invoices','transfers',
  'events','event_types','employee_roles','accounts','cash_shifts'));
ALTER TABLE chaika.sales_report_sets ADD COLUMN reviewed_at timestamptz;
ALTER TABLE chaika.sales_report_sets ADD COLUMN reviewed_by uuid REFERENCES chaika.web_users(id);
CREATE TABLE chaika.cash_shift_observations (
    snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    id uuid NOT NULL,
    session_number integer NOT NULL,
    fiscal_number integer,
    cash_reg_number integer NOT NULL,
    cash_reg_serial text,
    open_date timestamp NOT NULL,
    close_date timestamp,
    accept_date timestamp,
    manager_id uuid,
    responsible_user_id uuid,
    session_start_cash numeric,
    pay_orders numeric,
    sum_writeoff_orders numeric,
    sales_cash numeric,
    sales_credit numeric,
    sales_card numeric,
    pay_in numeric,
    pay_out numeric,
    pay_income numeric,
    cash_remain numeric,
    cash_diff numeric,
    session_status text NOT NULL,
    conception_id uuid,
    point_of_sale_id uuid,
    department_id uuid,
    group_id uuid,
    point_of_sale_name text,
    mapping_state text NOT NULL CHECK(mapping_state IN ('matched','missing','ambiguous','unknown_department')),
    groups_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    PRIMARY KEY(snapshot_id,id),
    CHECK((mapping_state='matched') = (department_id IS NOT NULL AND group_id IS NOT NULL))
);
CREATE INDEX cash_shift_department_idx ON chaika.cash_shift_observations(department_id,open_date);
CREATE INDEX cash_shift_group_snapshot_idx ON chaika.cash_shift_observations(groups_snapshot_id);
CREATE TABLE chaika.cash_shift_days (
    source_id text NOT NULL REFERENCES chaika.sources(id),
    open_day date NOT NULL,
    last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    observed_at timestamptz NOT NULL,
    row_count integer NOT NULL CHECK(row_count>=0),
    PRIMARY KEY(source_id,open_day)
);
CREATE INDEX cash_shift_day_snapshot_idx ON chaika.cash_shift_days(last_snapshot_id);
CREATE VIEW chaika.cash_shifts WITH (security_invoker=true) AS
SELECT DISTINCT ON (d.source_id,o.id) o.*,d.source_id,
       d.observed_at AS last_seen_at,d.last_snapshot_id
FROM chaika.cash_shift_days d
JOIN chaika.cash_shift_observations o ON o.snapshot_id=d.last_snapshot_id
WHERE o.open_date::date=d.open_day
ORDER BY d.source_id,o.id,d.observed_at DESC;
REVOKE ALL ON chaika.cash_shift_observations,chaika.cash_shift_days,chaika.cash_shifts
 FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT ON chaika.cash_shift_observations TO chaika_backend;
GRANT SELECT,INSERT,UPDATE ON chaika.cash_shift_days TO chaika_backend;
GRANT SELECT ON chaika.cash_shifts TO chaika_backend;
ALTER TABLE chaika.cash_shift_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.cash_shift_days ENABLE ROW LEVEL SECURITY;
CREATE POLICY backend_append ON chaika.cash_shift_observations TO chaika_backend
 USING(true) WITH CHECK(true);
CREATE POLICY backend_access ON chaika.cash_shift_days TO chaika_backend
 USING(true) WITH CHECK(true);
