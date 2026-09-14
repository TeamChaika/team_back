-- Full current role catalogue; role UUIDs in employees remain source-owned references.
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(7623011091001);
ALTER TABLE chaika.sync_runs DROP CONSTRAINT sync_runs_job_check;
ALTER TABLE chaika.sync_runs ADD CONSTRAINT sync_runs_job_check CHECK(job IN
 ('references','inventory','employees','store_balances','dictionaries','counteragent_balances',
  'events','employee_roles'));
ALTER TABLE chaika.raw_snapshots DROP CONSTRAINT raw_snapshots_resource_check;
ALTER TABLE chaika.raw_snapshots ADD CONSTRAINT raw_snapshots_resource_check CHECK(resource IN
 ('server_type','departments','groups','stores','replication','products','product_groups',
  'incoming_invoices','writeoffs','assembly_charts','employees','store_balances','counteragents',
  'measure_units','product_categories','counteragent_balances','outgoing_invoices','transfers',
  'events','event_types','employee_roles'));

CREATE TABLE chaika.employee_roles (
 source_id text NOT NULL REFERENCES chaika.sources(id), id uuid NOT NULL,
 code text NOT NULL, name text NOT NULL,
 payment_per_hour numeric, steady_salary numeric, schedule_type text, deleted boolean,
 present_in_latest boolean NOT NULL DEFAULT true,
 first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
 last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id), details jsonb NOT NULL,
 PRIMARY KEY(source_id,id)
);
CREATE INDEX employee_roles_code_idx ON chaika.employee_roles(source_id,code);
CREATE INDEX employee_roles_snapshot_idx ON chaika.employee_roles(last_snapshot_id);
COMMENT ON COLUMN chaika.employee_roles.code IS 'Original code; empty and duplicate codes are valid.';
COMMENT ON COLUMN chaika.employee_roles.present_in_latest IS
 'Presence in latest full export. Absence is not evidence of deletion.';
REVOKE ALL ON chaika.employee_roles FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE ON chaika.employee_roles TO chaika_backend;
ALTER TABLE chaika.employee_roles ENABLE ROW LEVEL SECURITY;
CREATE POLICY backend_access ON chaika.employee_roles TO chaika_backend
 USING(true) WITH CHECK(true);

-- Main role and list entries are separate evidence. Never zip UUID and code arrays.
CREATE VIEW chaika.employee_role_assignments WITH (security_invoker=true) AS
SELECT e.source_id,e.id AS employee_id,e.code AS employee_code,e.name AS employee_name,
 e.present_in_latest AS employee_present_in_latest,
 assignment.kind AS assignment_kind,assignment.ordinal,assignment.role_id,
 r.name AS role_name,r.code AS role_code,r.deleted AS role_deleted,
 r.id IS NOT NULL AS role_resolved,r.present_in_latest AS role_present_in_latest
FROM chaika.employees e
CROSS JOIN LATERAL (
 SELECT 'main'::text AS kind,0::bigint AS ordinal,e.main_role_id AS role_id
 WHERE e.main_role_id IS NOT NULL
 UNION ALL
 SELECT 'list'::text,a.ordinal,a.role_id
 FROM unnest(e.role_ids) WITH ORDINALITY AS a(role_id,ordinal)
 WHERE a.role_id IS NOT NULL
) assignment
LEFT JOIN chaika.employee_roles r ON r.source_id=e.source_id AND r.id=assignment.role_id;
REVOKE ALL ON chaika.employee_role_assignments FROM PUBLIC,anon,authenticated;
GRANT SELECT ON chaika.employee_role_assignments TO chaika_backend;
