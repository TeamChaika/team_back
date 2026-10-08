-- RestControl private registry on the existing Supabase PostgreSQL instance.
-- Additive: never modifies dashboard grants, accounts, documents or auth passwords.
CREATE SCHEMA restcontrol;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='restcontrol_backend') THEN
    CREATE ROLE restcontrol_backend NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END $$;
REVOKE ALL ON SCHEMA restcontrol FROM PUBLIC,anon,authenticated;
GRANT USAGE ON SCHEMA restcontrol TO restcontrol_backend;

CREATE TABLE restcontrol.companies (
 id uuid PRIMARY KEY, slug text UNIQUE NOT NULL, domain text UNIQUE,
 name text NOT NULL, name_search text NOT NULL, status text NOT NULL CHECK(status IN ('draft','active','suspended')),
 version integer NOT NULL CHECK(version>0), archived_at text, body jsonb NOT NULL,
 CHECK (body->>'id'=id::text), CHECK ((body->>'version')::integer=version),
 CHECK (body->>'slug'=slug), CHECK (body->>'status'=status),
 CHECK (body->>'name'=name), CHECK ((body->>'domain') IS NOT DISTINCT FROM domain),
 CHECK ((body->>'archived_at') IS NOT DISTINCT FROM archived_at));
CREATE TABLE IF NOT EXISTS restcontrol.platform_memberships (
 id uuid PRIMARY KEY, auth_user_id uuid NOT NULL UNIQUE, username text NOT NULL UNIQUE,
 display_name text NOT NULL, active boolean NOT NULL DEFAULT true);
CREATE TABLE IF NOT EXISTS restcontrol.memberships (
 id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES restcontrol.companies(id),
 auth_user_id uuid, username text NOT NULL, display_name text NOT NULL,
 role text NOT NULL DEFAULT 'company_admin' CHECK (role IN ('company_admin','employee')),
 is_primary_admin boolean NOT NULL DEFAULT true,
 active boolean NOT NULL DEFAULT true, auth_exclusive boolean NOT NULL DEFAULT false,
 must_change boolean NOT NULL DEFAULT false, temporary_expires double precision);
CREATE UNIQUE INDEX IF NOT EXISTS membership_primary_admin ON restcontrol.memberships(company_id) WHERE is_primary_admin;
CREATE UNIQUE INDEX IF NOT EXISTS membership_identity_company ON restcontrol.memberships(company_id,auth_user_id);
CREATE TABLE IF NOT EXISTS restcontrol.sessions (
 token_hash text PRIMARY KEY, owner_id uuid NOT NULL REFERENCES restcontrol.platform_memberships(id),
 csrf text NOT NULL, expires double precision NOT NULL, tokens_ciphertext text NOT NULL);
CREATE TABLE IF NOT EXISTS restcontrol.tenant_sessions (
 token_hash text PRIMARY KEY, admin_id uuid NOT NULL REFERENCES restcontrol.memberships(id),
 csrf text NOT NULL, expires double precision NOT NULL, tokens_ciphertext text NOT NULL);
CREATE TABLE IF NOT EXISTS restcontrol.attempts (
 key text PRIMARY KEY, count integer NOT NULL, until double precision NOT NULL);
CREATE TABLE IF NOT EXISTS restcontrol.auth_provisioning (
 company_id uuid PRIMARY KEY REFERENCES restcontrol.companies(id), request_id uuid NOT NULL UNIQUE,
 username text NOT NULL, state text NOT NULL, auth_user_id uuid, created_at text NOT NULL, temporary_ciphertext text);
CREATE TABLE IF NOT EXISTS restcontrol.tenant_events (
 id uuid PRIMARY KEY,company_id uuid NOT NULL REFERENCES restcontrol.companies(id),
 admin_id uuid NOT NULL REFERENCES restcontrol.memberships(id),action text NOT NULL,created_at text NOT NULL);
ALTER TABLE restcontrol.platform_memberships ADD CONSTRAINT platform_auth_user_fk
 FOREIGN KEY(auth_user_id) REFERENCES auth.users(id);
ALTER TABLE restcontrol.memberships ADD CONSTRAINT membership_auth_user_fk
 FOREIGN KEY(auth_user_id) REFERENCES auth.users(id);
CREATE TABLE restcontrol.events (
 id uuid PRIMARY KEY, company_id uuid NOT NULL REFERENCES restcontrol.companies(id),
 actor_id uuid NOT NULL REFERENCES restcontrol.platform_memberships(id), actor_name text NOT NULL,
 action text NOT NULL, changed_fields jsonb NOT NULL, created_at text NOT NULL);
CREATE INDEX events_company_time ON restcontrol.events(company_id,created_at DESC,id);
CREATE TABLE restcontrol.connections (
 company_id uuid NOT NULL REFERENCES restcontrol.companies(id), connection_id text NOT NULL,
 url text NOT NULL, ciphertext text NOT NULL, check_json jsonb NOT NULL,
 PRIMARY KEY(company_id,connection_id));
CREATE TABLE restcontrol.check_attempts (
 owner_id uuid PRIMARY KEY REFERENCES restcontrol.platform_memberships(id),
 count integer NOT NULL, until double precision NOT NULL);
CREATE TABLE restcontrol.check_targets (target text PRIMARY KEY,until double precision NOT NULL);
CREATE TABLE restcontrol.imports (
 source_digest text PRIMARY KEY, imported_at timestamptz NOT NULL DEFAULT now(),
 manifest jsonb NOT NULL);
-- Intentionally privileged narrow view: backend may resolve identity without
-- reading Auth hashes, tokens, metadata or changing any auth.users row.
CREATE VIEW restcontrol.auth_identities WITH (security_barrier=true) AS
 SELECT id,email,raw_app_meta_data->>'restcontrol_request_id' AS provision_id FROM auth.users;
REVOKE ALL ON restcontrol.auth_identities FROM PUBLIC,anon,authenticated;
GRANT SELECT ON restcontrol.auth_identities TO restcontrol_backend;
-- Browser roles cannot access any registry tables. RLS expresses the trusted
-- backend schema boundary; tenant row authorization remains an API responsibility.
DO $$ DECLARE t record; BEGIN
 FOR t IN SELECT tablename FROM pg_tables WHERE schemaname='restcontrol' LOOP
  EXECUTE format('ALTER TABLE restcontrol.%I ENABLE ROW LEVEL SECURITY',t.tablename);
  EXECUTE format('REVOKE ALL ON restcontrol.%I FROM PUBLIC,anon,authenticated',t.tablename);
  EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON restcontrol.%I TO restcontrol_backend',t.tablename);
  EXECUTE format('CREATE POLICY backend_access ON restcontrol.%I TO restcontrol_backend USING(true) WITH CHECK(true)',t.tablename);
 END LOOP;
END $$;
-- Audit is append-only to runtime; migration manifest is readonly.
REVOKE UPDATE,DELETE ON restcontrol.events,restcontrol.tenant_events FROM restcontrol_backend;
REVOKE INSERT,UPDATE,DELETE ON restcontrol.imports FROM restcontrol_backend;
-- Platform grants are changed only by the migration/operator connection.
REVOKE INSERT,UPDATE,DELETE ON restcontrol.platform_memberships FROM restcontrol_backend;
ALTER DEFAULT PRIVILEGES IN SCHEMA restcontrol REVOKE ALL ON TABLES FROM PUBLIC,anon,authenticated;
