-- One-day RMS observations and rebuildable order links. No source data in migrations.
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(7623011091001);
ALTER TABLE chaika.sync_runs DROP CONSTRAINT sync_runs_job_check;
ALTER TABLE chaika.sync_runs ADD CONSTRAINT sync_runs_job_check CHECK (job IN
 ('references','inventory','employees','store_balances','dictionaries','counteragent_balances','events'));
ALTER TABLE chaika.raw_snapshots DROP CONSTRAINT raw_snapshots_resource_check;
ALTER TABLE chaika.raw_snapshots ADD CONSTRAINT raw_snapshots_resource_check CHECK (resource IN
 ('server_type','departments','groups','stores','replication','products','product_groups',
 'incoming_invoices','writeoffs','assembly_charts','employees','store_balances','counteragents',
 'measure_units','product_categories','counteragent_balances','outgoing_invoices','transfers',
 'events','event_types'));

CREATE TABLE chaika.rms_event_versions (
 source_id text NOT NULL REFERENCES chaika.sources(id), event_id uuid NOT NULL,
 version_id uuid NOT NULL UNIQUE, version_no integer NOT NULL CHECK(version_no>0),
 observed_at timestamptz NOT NULL, snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
 content_hash text NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'), payload jsonb NOT NULL,
 PRIMARY KEY(source_id,event_id,version_no), UNIQUE(source_id,event_id,version_id)
);
CREATE INDEX rms_event_versions_snapshot_idx ON chaika.rms_event_versions(snapshot_id);
CREATE TABLE chaika.rms_events (
 source_id text NOT NULL REFERENCES chaika.sources(id), id uuid NOT NULL,
 version_id uuid NOT NULL, occurred_at timestamptz NOT NULL, event_type text NOT NULL,
 order_id uuid, order_number text, department_code text,
 actor_id uuid, authorizer_id uuid, waiter_id uuid, terminal_id uuid,
 event_sum numeric, order_sum_after_discount numeric,
 first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
 PRIMARY KEY(source_id,id),
 FOREIGN KEY(source_id,id,version_id) REFERENCES chaika.rms_event_versions(source_id,event_id,version_id)
);
CREATE INDEX rms_events_order_idx ON chaika.rms_events(source_id,order_id,occurred_at,id);
CREATE INDEX rms_events_number_idx ON chaika.rms_events(source_id,order_number,occurred_at);
CREATE INDEX rms_events_transfer_idx ON chaika.rms_events(source_id,occurred_at)
 WHERE event_type IN ('dishesMovedFrom','dishesMovedTo');
CREATE INDEX rms_events_version_idx ON chaika.rms_events(version_id);
CREATE TABLE chaika.rms_event_observations (
 snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
 source_id text NOT NULL, event_id uuid NOT NULL, version_id uuid NOT NULL,
 PRIMARY KEY(snapshot_id,event_id),
 FOREIGN KEY(source_id,event_id,version_id) REFERENCES chaika.rms_event_versions(source_id,event_id,version_id)
);
CREATE INDEX rms_event_observations_version_idx ON chaika.rms_event_observations(version_id);
CREATE TABLE chaika.rms_event_days (
 source_id text NOT NULL REFERENCES chaika.sources(id), event_date date NOT NULL,
 timezone text NOT NULL, last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
 event_count integer NOT NULL CHECK(event_count>=0), observed_at timestamptz NOT NULL,
 PRIMARY KEY(source_id,event_date)
);
CREATE INDEX rms_event_days_snapshot_idx ON chaika.rms_event_days(last_snapshot_id);
CREATE TABLE chaika.rms_event_types (
 source_id text NOT NULL REFERENCES chaika.sources(id), id text NOT NULL,
 label text NOT NULL, details jsonb NOT NULL,
 last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
 PRIMARY KEY(source_id,id)
);
CREATE INDEX rms_event_types_snapshot_idx ON chaika.rms_event_types(last_snapshot_id);
CREATE TABLE chaika.rms_event_links (
 source_id text NOT NULL, event_id uuid NOT NULL, paired_event_id uuid,
 status text NOT NULL CHECK(status IN ('matched','pending','ambiguous','invalid')),
 candidate_ids jsonb NOT NULL, algorithm_version text NOT NULL, evidence jsonb NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(source_id,event_id),
 FOREIGN KEY(source_id,event_id) REFERENCES chaika.rms_events(source_id,id),
 FOREIGN KEY(source_id,paired_event_id) REFERENCES chaika.rms_events(source_id,id),
 CHECK((status='matched')=(paired_event_id IS NOT NULL)), CHECK(event_id<>paired_event_id)
);
CREATE INDEX rms_event_links_paired_idx ON chaika.rms_event_links(source_id,paired_event_id);
COMMENT ON TABLE chaika.rms_event_versions IS
 'Append-only observed business versions, including A-B-A. Credential attributes are redacted; original XML remains private locally.';
COMMENT ON TABLE chaika.rms_event_links IS
 'Rebuildable hypotheses. matched means unique reciprocal event matching, not a source-provided order-line identity.';
DO $policies$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY['rms_event_versions','rms_event_observations','rms_events',
 'rms_event_days','rms_event_types','rms_event_links'] LOOP
  EXECUTE format('REVOKE ALL ON chaika.%I FROM PUBLIC,anon,authenticated',t);
  EXECUTE format('ALTER TABLE chaika.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('GRANT SELECT,INSERT ON chaika.%I TO chaika_backend',t);
  EXECUTE format('CREATE POLICY backend_read ON chaika.%I FOR SELECT TO chaika_backend USING(true)',t);
  EXECUTE format('CREATE POLICY backend_append ON chaika.%I FOR INSERT TO chaika_backend WITH CHECK(true)',t);
  IF t NOT IN ('rms_event_versions','rms_event_observations') THEN
   EXECUTE format('GRANT UPDATE ON chaika.%I TO chaika_backend',t);
   EXECUTE format('CREATE POLICY backend_update ON chaika.%I FOR UPDATE TO chaika_backend USING(true) WITH CHECK(true)',t);
  END IF;
 END LOOP;
END
$policies$;
