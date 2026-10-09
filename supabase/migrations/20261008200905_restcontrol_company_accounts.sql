-- Central-only employee provisioning journal. Never stores a password or JWT.
CREATE TABLE restcontrol.company_account_requests (
 request_id uuid PRIMARY KEY,
 company_id uuid NOT NULL REFERENCES restcontrol.companies(id),
 actor_id uuid NOT NULL,
 email text NOT NULL,
 display_name text NOT NULL,
 fingerprint text NOT NULL,
 proof_key_ciphertext text NOT NULL,
 state text NOT NULL CHECK(state IN ('pending','identity_ready','complete','rejected')),
 auth_user_id uuid,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX company_account_active_email ON restcontrol.company_account_requests(company_id,email)
 WHERE state<>'rejected';
ALTER TABLE restcontrol.company_account_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE restcontrol.company_account_requests FORCE ROW LEVEL SECURITY;
REVOKE ALL ON restcontrol.company_account_requests FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE ON restcontrol.company_account_requests TO restcontrol_backend;
CREATE POLICY backend_access ON restcontrol.company_account_requests TO restcontrol_backend
 USING(true) WITH CHECK(true);
