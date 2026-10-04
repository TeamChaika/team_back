-- Separate manual requests: automatic schedule slots remain untouched.
CREATE TABLE chaika.manual_sync_requests (
    request_id uuid PRIMARY KEY,
    job text NOT NULL,
    requested_by uuid NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','running','succeeded','failed')),
    started_at timestamptz,
    finished_at timestamptz,
    error_code text
);
CREATE INDEX manual_sync_latest ON chaika.manual_sync_requests(job,requested_at DESC);
CREATE UNIQUE INDEX manual_sync_active ON chaika.manual_sync_requests(job)
    WHERE state IN ('pending','running');
CREATE TABLE chaika.scheduler_runtime (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    instance_id uuid NOT NULL,
    heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    available boolean NOT NULL DEFAULT true
);
ALTER TABLE chaika.manual_sync_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.scheduler_runtime ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON chaika.manual_sync_requests,chaika.scheduler_runtime FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE ON chaika.manual_sync_requests,chaika.scheduler_runtime
    TO chaika_backend;
CREATE POLICY backend_access ON chaika.manual_sync_requests TO chaika_backend
    USING(true) WITH CHECK(true);
CREATE POLICY backend_access ON chaika.scheduler_runtime TO chaika_backend
    USING(true) WITH CHECK(true);
