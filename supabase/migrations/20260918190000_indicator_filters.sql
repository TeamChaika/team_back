CREATE TABLE chaika.indicator_filter_values (
    source_id text NOT NULL,
    field text NOT NULL,
    department_id uuid NOT NULL,
    value text NOT NULL CHECK (length(value) BETWEEN 1 AND 500),
    PRIMARY KEY (source_id, field, department_id, value)
);
CREATE TABLE chaika.indicator_filter_sync (
    source_id text NOT NULL,
    field text NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    synced_at timestamptz NOT NULL DEFAULT now(),
    value_count integer NOT NULL CHECK(value_count >= 0),
    PRIMARY KEY (source_id, field)
);
ALTER TABLE chaika.indicator_filter_values ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.indicator_filter_sync ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON chaika.indicator_filter_values, chaika.indicator_filter_sync FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON chaika.indicator_filter_values, chaika.indicator_filter_sync TO chaika_backend;
CREATE POLICY backend_access ON chaika.indicator_filter_values TO chaika_backend USING(true) WITH CHECK(true);
CREATE POLICY backend_access ON chaika.indicator_filter_sync TO chaika_backend USING(true) WITH CHECK(true);

CREATE INDEX indicator_filters_department ON chaika.indicator_filter_values(department_id,field);
