-- Full source-owned account dictionary, with nullable parent links retained verbatim.
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(7623011091001);
ALTER TABLE chaika.sync_runs DROP CONSTRAINT sync_runs_job_check;
ALTER TABLE chaika.sync_runs ADD CONSTRAINT sync_runs_job_check CHECK(job IN
 ('references','inventory','employees','store_balances','dictionaries','counteragent_balances',
  'events','employee_roles','accounts'));
ALTER TABLE chaika.raw_snapshots DROP CONSTRAINT raw_snapshots_resource_check;
ALTER TABLE chaika.raw_snapshots ADD CONSTRAINT raw_snapshots_resource_check CHECK(resource IN
 ('server_type','departments','groups','stores','replication','products','product_groups',
  'incoming_invoices','writeoffs','assembly_charts','employees','store_balances','counteragents',
  'measure_units','product_categories','counteragent_balances','outgoing_invoices','transfers',
  'events','event_types','employee_roles','accounts'));

CREATE TABLE chaika.accounts (
 source_id text NOT NULL REFERENCES chaika.sources(id), id uuid NOT NULL,
 root_type text NOT NULL CHECK(root_type='Account'), code text, name text NOT NULL,
 deleted boolean NOT NULL, account_parent_id uuid, parent_corporate_id uuid,
 type text NOT NULL, system boolean NOT NULL, custom_transactions_allowed boolean NOT NULL,
 present_in_latest boolean NOT NULL DEFAULT true,
 first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
 last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id), details jsonb NOT NULL,
 PRIMARY KEY(source_id,id)
);
CREATE INDEX accounts_parent_idx ON chaika.accounts(source_id,account_parent_id);
CREATE INDEX accounts_corporate_idx ON chaika.accounts(source_id,parent_corporate_id);
CREATE INDEX accounts_snapshot_idx ON chaika.accounts(last_snapshot_id);
COMMENT ON COLUMN chaika.accounts.present_in_latest IS
 'Presence in latest full export. Absence is not evidence of deletion.';
COMMENT ON COLUMN chaika.accounts.type IS
 'Original iiko account type, including future codes. Not a rule for interpreting balance signs.';
COMMENT ON COLUMN chaika.accounts.account_parent_id IS
 'Original parent account UUID. Missing parents are reported, not discarded or fabricated.';
REVOKE ALL ON chaika.accounts FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE ON chaika.accounts TO chaika_backend;
ALTER TABLE chaika.accounts ENABLE ROW LEVEL SECURITY;
CREATE POLICY backend_access ON chaika.accounts TO chaika_backend USING(true) WITH CHECK(true);

-- Exactly one row per source report line. Names/types are from the current dictionary;
-- the original report snapshot and signed NUMERIC sum remain intact.
CREATE VIEW chaika.counteragent_balances_with_accounts WITH (security_invoker=true) AS
SELECT r.source_id,r.accounting_timestamp,i.snapshot_id,i.line_num,
 i.account_id,a.name AS account_name,a.code AS account_code,a.type AS account_type,
 a.account_parent_id,a.parent_corporate_id,a.deleted AS account_deleted,
 a.id IS NOT NULL AS account_resolved,a.present_in_latest AS account_present_in_latest,
 i.counteragent_id,i.department_id,i.sum
FROM chaika.counteragent_balance_reports r
JOIN chaika.counteragent_balance_items i ON i.snapshot_id=r.last_snapshot_id
LEFT JOIN chaika.accounts a ON a.source_id=r.source_id AND a.id=i.account_id;
COMMENT ON VIEW chaika.counteragent_balances_with_accounts IS
 'Latest observation per accounting timestamp, enriched with current account names. Not a P&L report.';
REVOKE ALL ON chaika.counteragent_balances_with_accounts FROM PUBLIC,anon,authenticated;
GRANT SELECT ON chaika.counteragent_balances_with_accounts TO chaika_backend;
