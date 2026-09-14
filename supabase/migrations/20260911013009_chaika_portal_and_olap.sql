-- Private portal access and immutable, day-scoped OLAP observations.
CREATE TABLE chaika.web_users (
    id uuid PRIMARY KEY REFERENCES auth.users(id),
    display_name text NOT NULL CHECK(length(display_name) BETWEEN 1 AND 150),
    role text NOT NULL CHECK(role IN ('owner','manager','analyst')),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE chaika.web_department_access (
    user_id uuid NOT NULL REFERENCES chaika.web_users(id) ON DELETE CASCADE,
    source_id text NOT NULL,
    department_id uuid NOT NULL,
    PRIMARY KEY(user_id,source_id,department_id),
    FOREIGN KEY(source_id,department_id) REFERENCES chaika.corporate_nodes(source_id,id)
);
CREATE TABLE chaika.sales_report_sets (
    id uuid PRIMARY KEY,
    source_id text NOT NULL REFERENCES chaika.sources(id),
    business_date date NOT NULL,
    observed_at timestamptz NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(),
    reviewed boolean NOT NULL DEFAULT false,
    checks jsonb NOT NULL,
    UNIQUE(id,source_id,business_date)
);
CREATE TABLE chaika.sales_report_days (
    source_id text NOT NULL,
    business_date date NOT NULL,
    current_set_id uuid NOT NULL,
    PRIMARY KEY(source_id,business_date),
    FOREIGN KEY(current_set_id,source_id,business_date)
      REFERENCES chaika.sales_report_sets(id,source_id,business_date)
);
CREATE TABLE chaika.sales_reports (
    id uuid PRIMARY KEY,
    set_id uuid NOT NULL REFERENCES chaika.sales_report_sets(id),
    kind text NOT NULL CHECK(kind IN ('daily','dishes','payments','discounts','returns','waiters','hours')),
    request jsonb NOT NULL,
    observed_at timestamptz NOT NULL,
    raw bytea NOT NULL,
    sha256 text NOT NULL CHECK(encode(sha256(raw),'hex')=sha256),
    row_count integer NOT NULL CHECK(row_count>=0),
    UNIQUE(set_id,kind)
);
CREATE TABLE chaika.sales_report_rows (
    report_id uuid NOT NULL REFERENCES chaika.sales_reports(id),
    ordinal integer NOT NULL CHECK(ordinal>=0),
    department_id uuid NOT NULL,
    revenue numeric NOT NULL,
    cost numeric,
    checks numeric,
    guests numeric,
    discount numeric,
    return_sum numeric,
    quantity numeric,
    dimensions jsonb NOT NULL,
    PRIMARY KEY(report_id,ordinal)
);
CREATE INDEX sales_rows_department_idx ON chaika.sales_report_rows(department_id,report_id);
CREATE INDEX sales_sets_date_idx ON chaika.sales_report_sets(source_id,business_date);
CREATE INDEX sales_reports_set_idx ON chaika.sales_reports(set_id);

REVOKE ALL ON chaika.web_users,chaika.web_department_access,chaika.sales_report_sets,
 chaika.sales_report_days,chaika.sales_reports,chaika.sales_report_rows FROM PUBLIC,anon,authenticated;
GRANT SELECT ON chaika.web_users,chaika.web_department_access TO chaika_backend;
GRANT SELECT,INSERT ON chaika.sales_report_sets,chaika.sales_reports,chaika.sales_report_rows TO chaika_backend;
GRANT SELECT,INSERT,UPDATE ON chaika.sales_report_days TO chaika_backend;
ALTER TABLE chaika.web_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.web_department_access ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.sales_report_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.sales_report_days ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.sales_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE chaika.sales_report_rows ENABLE ROW LEVEL SECURITY;
CREATE POLICY backend_read ON chaika.web_users FOR SELECT TO chaika_backend USING(true);
CREATE POLICY backend_read ON chaika.web_department_access FOR SELECT TO chaika_backend USING(true);
CREATE POLICY backend_append ON chaika.sales_report_sets TO chaika_backend USING(true) WITH CHECK(true);
CREATE POLICY backend_append ON chaika.sales_reports TO chaika_backend USING(true) WITH CHECK(true);
CREATE POLICY backend_append ON chaika.sales_report_rows TO chaika_backend USING(true) WITH CHECK(true);
CREATE POLICY backend_access ON chaika.sales_report_days TO chaika_backend USING(true) WITH CHECK(true);
