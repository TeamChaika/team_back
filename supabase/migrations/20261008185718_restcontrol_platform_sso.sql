-- Opaque, company-bound delegated handles. Refresh tokens stay in central sessions.
CREATE TABLE restcontrol.platform_sso_codes (
 code_hash text PRIMARY KEY,
 parent_hash text NOT NULL REFERENCES restcontrol.sessions(token_hash) ON DELETE CASCADE,
 company_id uuid NOT NULL REFERENCES restcontrol.companies(id),
 frontend_origin text NOT NULL, api_origin text NOT NULL,
 state text NOT NULL, nonce text NOT NULL, challenge text NOT NULL,
 expires double precision NOT NULL
);
CREATE TABLE restcontrol.platform_tenant_sessions (
 token_hash text PRIMARY KEY,
 parent_hash text NOT NULL REFERENCES restcontrol.sessions(token_hash) ON DELETE CASCADE,
 company_id uuid NOT NULL REFERENCES restcontrol.companies(id),
 frontend_origin text NOT NULL, api_origin text NOT NULL,
 csrf text NOT NULL, expires double precision NOT NULL
);
CREATE TABLE restcontrol.platform_tenant_events (
 id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES restcontrol.companies(id),
 actor_id uuid NOT NULL, actor_name text NOT NULL,
 action text NOT NULL, object_id text, result text NOT NULL, created_at text NOT NULL
);
CREATE INDEX platform_tenant_sessions_company ON restcontrol.platform_tenant_sessions(company_id);
CREATE INDEX platform_sso_codes_expiry ON restcontrol.platform_sso_codes(expires);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['platform_sso_codes','platform_tenant_sessions','platform_tenant_events'] LOOP
 EXECUTE format('ALTER TABLE restcontrol.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('REVOKE ALL ON restcontrol.%I FROM PUBLIC,anon,authenticated',t);
 EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON restcontrol.%I TO restcontrol_backend',t);
 EXECUTE format('CREATE POLICY backend_access ON restcontrol.%I TO restcontrol_backend USING(true) WITH CHECK(true)',t);
 END LOOP;
END $$;
REVOKE UPDATE,DELETE ON restcontrol.platform_tenant_events FROM restcontrol_backend;
