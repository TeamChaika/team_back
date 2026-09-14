-- Immutable drilldown observations tied to the original, restaurant-scoped OLAP row.
-- Rollback: disable drilldown routes and retain observations; no existing data is rewritten.
SET LOCAL lock_timeout='3s';
CREATE TABLE chaika.sales_drilldown_captures (
    id uuid PRIMARY KEY,
    report_id uuid NOT NULL,
    ordinal integer NOT NULL,
    order_id uuid,
    request jsonb NOT NULL,
    observed_at timestamptz NOT NULL,
    raw bytea NOT NULL,
    sha256 text NOT NULL CHECK(encode(sha256(raw),'hex')=sha256),
    rows jsonb NOT NULL CHECK(jsonb_typeof(rows)='array' AND jsonb_array_length(rows)<=10000),
    FOREIGN KEY(report_id,ordinal) REFERENCES chaika.sales_report_rows(report_id,ordinal),
    UNIQUE NULLS NOT DISTINCT(report_id,ordinal,order_id)
);
REVOKE ALL ON chaika.sales_drilldown_captures FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT ON chaika.sales_drilldown_captures TO chaika_backend;
ALTER TABLE chaika.sales_drilldown_captures ENABLE ROW LEVEL SECURITY;
CREATE POLICY backend_read ON chaika.sales_drilldown_captures FOR SELECT TO chaika_backend USING(true);
CREATE POLICY backend_insert ON chaika.sales_drilldown_captures FOR INSERT TO chaika_backend WITH CHECK(true);
