-- One current employee dictionary per source, with immutable source observations.
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(7623011091001);

ALTER TABLE chaika.sync_runs DROP CONSTRAINT sync_runs_job_check;
ALTER TABLE chaika.sync_runs ADD CONSTRAINT sync_runs_job_check
    CHECK (job IN ('references', 'inventory', 'employees'));
ALTER TABLE chaika.raw_snapshots DROP CONSTRAINT raw_snapshots_resource_check;
ALTER TABLE chaika.raw_snapshots ADD CONSTRAINT raw_snapshots_resource_check CHECK (resource IN
    ('server_type','departments','groups','stores','replication','products','product_groups',
     'incoming_invoices','writeoffs','assembly_charts','employees'));

CREATE TABLE chaika.employees (
    source_id text NOT NULL REFERENCES chaika.sources(id),
    id uuid NOT NULL,
    code text NOT NULL,
    name text NOT NULL,
    first_name text, middle_name text, last_name text,
    main_role_id uuid, role_ids uuid[], main_role_code text, role_codes text[],
    preferred_department_code text,
    department_codes text[], department_codes_state text,
    responsibility_department_codes text[], responsibility_department_codes_state text,
    deleted boolean, employee boolean, supplier boolean, client boolean,
    present_in_latest boolean NOT NULL DEFAULT true,
    first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
    last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    details jsonb NOT NULL,
    PRIMARY KEY (source_id, id)
);
CREATE INDEX employees_code_idx ON chaika.employees(source_id, code);
CREATE INDEX employees_main_role_idx ON chaika.employees(source_id, main_role_id);
CREATE INDEX employees_snapshot_idx ON chaika.employees(last_snapshot_id);
COMMENT ON COLUMN chaika.employees.code IS 'Original iiko code; empty and duplicate values are valid.';
COMMENT ON COLUMN chaika.employees.present_in_latest IS
    'Presence in the last complete includeDeleted=false export; absence does not mean fired or deleted.';
COMMENT ON COLUMN chaika.employees.department_codes_state IS
    'Original departmentCodesState; NULL/EMPTY strings are retained without inferring access scope.';
-- Role UUIDs and department codes are kept even before the related dictionaries are loaded.
REVOKE ALL ON chaika.employees FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON chaika.employees TO chaika_backend;
ALTER TABLE chaika.employees ENABLE ROW LEVEL SECURITY;
CREATE POLICY backend_access ON chaika.employees TO chaika_backend
    USING (true) WITH CHECK (true);
