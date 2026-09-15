CREATE TABLE chaika.scheduled_sync_runs (
    job text NOT NULL,
    slot timestamptz NOT NULL,
    status text NOT NULL CHECK(status IN ('running','succeeded','failed')),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    attempts integer NOT NULL DEFAULT 1,
    error_code text,
    next_retry_at timestamptz,
    PRIMARY KEY(job,slot)
);
ALTER TABLE chaika.scheduled_sync_runs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON chaika.scheduled_sync_runs FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE ON chaika.scheduled_sync_runs TO chaika_backend;
CREATE POLICY backend_access ON chaika.scheduled_sync_runs TO chaika_backend
    USING(true) WITH CHECK(true);
