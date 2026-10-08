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
