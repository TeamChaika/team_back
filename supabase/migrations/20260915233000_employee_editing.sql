SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(7623011091001);

ALTER TABLE chaika.employees ADD COLUMN phone text;
ALTER TABLE chaika.employees ADD COLUMN cell_phone text;
ALTER TABLE chaika.employees ADD COLUMN email text;

-- Committed before the upstream write: a retry must reconcile, never send it again.
CREATE TABLE chaika.employee_changes (
 id uuid PRIMARY KEY,
 user_id uuid NOT NULL REFERENCES chaika.web_users(id),
 employee_id uuid NOT NULL,
 is_create boolean NOT NULL,
 request_hash text NOT NULL,
 fields jsonb NOT NULL,
 before_fields jsonb,
 status text NOT NULL CHECK (status IN ('pending','confirmed','rejected','reconciled')),
 created_at timestamptz NOT NULL DEFAULT now(),
 finished_at timestamptz,
 snapshot_id uuid REFERENCES chaika.raw_snapshots(id),
 error_code text
);
CREATE UNIQUE INDEX employee_changes_pending_idx ON chaika.employee_changes(employee_id)
 WHERE status='pending';
REVOKE ALL ON chaika.employee_changes FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE ON chaika.employee_changes TO chaika_backend;
ALTER TABLE chaika.employee_changes ENABLE ROW LEVEL SECURITY;
CREATE POLICY backend_access ON chaika.employee_changes TO chaika_backend
 USING (true) WITH CHECK (true);
