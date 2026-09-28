ALTER TABLE chaika.web_users
  ADD COLUMN sections text[] NOT NULL DEFAULT '{}',
  ADD COLUMN is_portal_admin boolean NOT NULL DEFAULT false,
  ADD COLUMN all_departments boolean NOT NULL DEFAULT false,
  ADD COLUMN revision integer NOT NULL DEFAULT 1;
UPDATE chaika.web_users SET sections = CASE WHEN role='deposits' THEN ARRAY['deposits']
  ELSE ARRAY['overview','indicators','sales','deposits','cash-shifts','invoices',
    'purchase-prices','outgoing','transfers','writeoffs','products','charts','balances',
    'employees','events','status'] END, all_departments=(role='owner');
CREATE VIEW chaika.portal_identities WITH (security_barrier=true) AS
  SELECT id,email,raw_app_meta_data->>'chaika_portal_request_id' AS provision_id
  FROM auth.users;
REVOKE ALL ON chaika.portal_identities FROM PUBLIC,anon,authenticated;
GRANT SELECT ON chaika.portal_identities TO chaika_backend;
GRANT INSERT,UPDATE,DELETE ON chaika.web_users,chaika.web_department_access TO chaika_backend;
CREATE POLICY backend_write ON chaika.web_users FOR ALL TO chaika_backend USING(true) WITH CHECK(true);
CREATE POLICY backend_write ON chaika.web_department_access FOR ALL TO chaika_backend USING(true) WITH CHECK(true);
CREATE TABLE chaika.portal_admin_audit (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  actor_id uuid NOT NULL, action text NOT NULL, target_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE chaika.portal_admin_audit ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON chaika.portal_admin_audit FROM PUBLIC,anon,authenticated;
GRANT INSERT,SELECT ON chaika.portal_admin_audit TO chaika_backend;
GRANT USAGE ON SEQUENCE chaika.portal_admin_audit_id_seq TO chaika_backend;
CREATE POLICY backend_access ON chaika.portal_admin_audit TO chaika_backend USING(true) WITH CHECK(true);
