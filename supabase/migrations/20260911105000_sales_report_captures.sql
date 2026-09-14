-- Additive storage for one immutable source response shared by up to seven days.
-- Application rollback: stop the weekly loader and use the previous daily loader;
-- retain this table and the nullable column so existing captures are not lost.
SET LOCAL lock_timeout='3s';
CREATE TABLE chaika.sales_report_captures (
    id uuid PRIMARY KEY,
    source_id text NOT NULL REFERENCES chaika.sources(id),
    kind text NOT NULL CHECK(kind IN ('daily','dishes','payments','discounts','returns','waiters','hours')),
    date_from date NOT NULL,
    date_to date NOT NULL CHECK(date_to >= date_from AND date_to-date_from <= 6),
    request jsonb NOT NULL,
    observed_at timestamptz NOT NULL,
    raw bytea NOT NULL,
    sha256 text NOT NULL CHECK(encode(sha256(raw),'hex')=sha256),
    row_count integer NOT NULL CHECK(row_count>=0),
    UNIQUE(id,kind,sha256)
);
ALTER TABLE chaika.sales_reports ADD COLUMN source_capture_id uuid;
ALTER TABLE chaika.sales_reports ALTER COLUMN raw DROP NOT NULL;
ALTER TABLE chaika.sales_reports ADD CONSTRAINT sales_reports_raw_origin CHECK (
    (raw IS NOT NULL AND source_capture_id IS NULL) OR
    (raw IS NULL AND source_capture_id IS NOT NULL)
);
ALTER TABLE chaika.sales_reports ADD CONSTRAINT sales_reports_capture_fk
    FOREIGN KEY(source_capture_id,kind,sha256)
    REFERENCES chaika.sales_report_captures(id,kind,sha256);
REVOKE ALL ON chaika.sales_report_captures FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT ON chaika.sales_report_captures TO chaika_backend;
ALTER TABLE chaika.sales_report_captures ENABLE ROW LEVEL SECURITY;
CREATE POLICY backend_read ON chaika.sales_report_captures FOR SELECT TO chaika_backend USING(true);
CREATE POLICY backend_insert ON chaika.sales_report_captures FOR INSERT TO chaika_backend WITH CHECK(true);
