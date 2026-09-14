-- First private reference store. No iiko data or passwords belong in this migration.
-- Forward-only after data loading; see docs/database-sync.md for rollback procedure.
CREATE ROLE chaika_backend LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE SCHEMA chaika;
REVOKE ALL ON SCHEMA chaika FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA chaika TO chaika_backend;
GRANT CONNECT ON DATABASE postgres TO chaika_backend;

CREATE TABLE chaika.sources (
    id text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9-]{0,49}$'),
    label text NOT NULL,
    base_url text NOT NULL UNIQUE,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    server_type text CHECK (server_type IN ('CHAIN', 'REPLICATED_RMS', 'STANDALONE_RMS')),
    verified_at timestamptz,
    configured boolean NOT NULL DEFAULT true
);

CREATE TABLE chaika.sync_runs (
    id uuid PRIMARY KEY,
    job text NOT NULL DEFAULT 'references' CHECK (job = 'references'),
    status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    counts jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_code text,
    CHECK ((status = 'running') = (finished_at IS NULL))
);

CREATE TABLE chaika.raw_snapshots (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES chaika.sync_runs(id),
    source_id text NOT NULL REFERENCES chaika.sources(id),
    resource text NOT NULL CHECK (resource IN
        ('server_type', 'departments', 'groups', 'stores', 'replication')),
    observed_at timestamptz NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    source_bytes integer NOT NULL CHECK (source_bytes >= 0),
    raw bytea NOT NULL,
    normalized jsonb NOT NULL,
    CHECK (octet_length(raw) = source_bytes),
    CHECK (encode(sha256(raw), 'hex') = sha256)
);
CREATE INDEX raw_snapshots_run_idx ON chaika.raw_snapshots(run_id);
CREATE INDEX raw_snapshots_source_resource_time_idx
    ON chaika.raw_snapshots(source_id, resource, observed_at DESC);

CREATE TABLE chaika.corporate_nodes (
    source_id text NOT NULL REFERENCES chaika.sources(id),
    id uuid NOT NULL,
    parent_id uuid,
    code text,
    name text,
    type text NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    present_in_latest boolean NOT NULL DEFAULT true,
    last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    PRIMARY KEY (source_id, id),
    FOREIGN KEY (source_id, parent_id) REFERENCES chaika.corporate_nodes(source_id, id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX corporate_nodes_parent_idx ON chaika.corporate_nodes(source_id, parent_id);
CREATE INDEX corporate_nodes_snapshot_idx ON chaika.corporate_nodes(last_snapshot_id);

CREATE TABLE chaika.stores (
    source_id text NOT NULL REFERENCES chaika.sources(id),
    id uuid NOT NULL,
    parent_id uuid,
    code text,
    name text,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    present_in_latest boolean NOT NULL DEFAULT true,
    last_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    PRIMARY KEY (source_id, id),
    FOREIGN KEY (source_id, parent_id) REFERENCES chaika.corporate_nodes(source_id, id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX stores_parent_idx ON chaika.stores(source_id, parent_id);
CREATE INDEX stores_snapshot_idx ON chaika.stores(last_snapshot_id);

CREATE TABLE chaika.rms_bindings (
    source_id text PRIMARY KEY REFERENCES chaika.sources(id),
    chain_source_id text NOT NULL REFERENCES chaika.sources(id),
    department_id uuid,
    state text NOT NULL,
    details jsonb NOT NULL,
    observed_at timestamptz NOT NULL,
    chain_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    rms_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    groups_snapshot_id uuid NOT NULL REFERENCES chaika.raw_snapshots(id),
    CHECK (source_id <> chain_source_id),
    CHECK (state <> 'matched' OR department_id IS NOT NULL),
    FOREIGN KEY (chain_source_id, department_id)
        REFERENCES chaika.corporate_nodes(source_id, id)
);
CREATE UNIQUE INDEX rms_bindings_matched_department_idx
    ON chaika.rms_bindings(chain_source_id, department_id) WHERE state = 'matched';
CREATE INDEX rms_bindings_department_idx ON chaika.rms_bindings(chain_source_id, department_id);
CREATE INDEX rms_bindings_chain_snapshot_idx ON chaika.rms_bindings(chain_snapshot_id);
CREATE INDEX rms_bindings_rms_snapshot_idx ON chaika.rms_bindings(rms_snapshot_id);
CREATE INDEX rms_bindings_groups_snapshot_idx ON chaika.rms_bindings(groups_snapshot_id);

REVOKE ALL ON ALL TABLES IN SCHEMA chaika FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON chaika.sources, chaika.sync_runs,
    chaika.corporate_nodes, chaika.stores, chaika.rms_bindings TO chaika_backend;
GRANT SELECT, INSERT ON chaika.raw_snapshots TO chaika_backend;

ALTER TABLE chaika.sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.sync_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.raw_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.corporate_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.stores ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.rms_bindings ENABLE ROW LEVEL SECURITY;
CREATE POLICY backend_access ON chaika.sources TO chaika_backend USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON chaika.sync_runs TO chaika_backend USING (true) WITH CHECK (true);
CREATE POLICY backend_read ON chaika.raw_snapshots FOR SELECT TO chaika_backend USING (true);
CREATE POLICY backend_append ON chaika.raw_snapshots FOR INSERT TO chaika_backend WITH CHECK (true);
CREATE POLICY backend_access ON chaika.corporate_nodes TO chaika_backend USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON chaika.stores TO chaika_backend USING (true) WITH CHECK (true);
CREATE POLICY backend_access ON chaika.rms_bindings TO chaika_backend USING (true) WITH CHECK (true);
