-- Last-good exact-scope indicators; no client role may read cached business values.
CREATE TABLE chaika.indicator_cache (
    key text PRIMARY KEY CHECK(length(key)=64),
    spec jsonb NOT NULL,
    user_id uuid NOT NULL,
    selection uuid[] NOT NULL DEFAULT '{}',
    payload jsonb,
    observed_at timestamptz,
    requested_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now()+interval '24 hours',
    due_at timestamptz NOT NULL DEFAULT now(),
    lease_token uuid,
    lease_until timestamptz,
    last_error text,
    CHECK ((payload IS NULL) = (observed_at IS NULL))
);
CREATE INDEX indicator_cache_due ON chaika.indicator_cache(due_at);
ALTER TABLE chaika.indicator_cache ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON chaika.indicator_cache FROM PUBLIC, anon, authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON chaika.indicator_cache TO chaika_backend;
CREATE POLICY backend_access ON chaika.indicator_cache TO chaika_backend USING(true) WITH CHECK(true);
