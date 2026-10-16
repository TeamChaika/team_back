-- Additive isolated support schema. Run as database administrator.
-- No public PostgREST exposure, auth changes or grants on business schemas.
BEGIN;
CREATE SCHEMA IF NOT EXISTS support;
REVOKE ALL ON SCHEMA support FROM PUBLIC,anon,authenticated;
CREATE TABLE support.principals (
    role_name name PRIMARY KEY,
    company_id text,
    is_platform boolean NOT NULL DEFAULT false,
    CHECK ((is_platform AND company_id IS NULL) OR (NOT is_platform AND company_id IS NOT NULL))
);
REVOKE ALL ON support.principals FROM PUBLIC,anon,authenticated;
INSERT INTO support.principals(role_name,is_platform) VALUES ('restcontrol_backend',true);
CREATE FUNCTION support.can_access(target_company text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,support AS $$
 SELECT EXISTS (SELECT 1 FROM support.principals p WHERE p.role_name=session_user
   AND (p.is_platform OR p.company_id=target_company))
$$;
REVOKE ALL ON FUNCTION support.can_access(text) FROM PUBLIC;
CREATE TABLE support.events (
    id uuid PRIMARY KEY,
    company_id text NOT NULL,
    company_name text NOT NULL,
    actor_id text NOT NULL,
    actor_name text NOT NULL DEFAULT 'Система',
    source text NOT NULL CHECK(source IN ('browser','server','background')),
    code text NOT NULL CHECK(code IN ('browser_error','unhandled_rejection','api_error','http_5xx','unhandled_exception','sync_failed','document_failed')),
    screen text NOT NULL CHECK(screen IN ('overview','sales','reports','documents','transfers','writeoffs','incoming','outgoing','deposits','management','profile','other')),
    status_code integer CHECK(status_code BETWEEN 400 AND 599),
    request_id uuid,
    diagnostic jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(octet_length(diagnostic::text)<5000),
    fingerprint text NOT NULL,
    bucket timestamptz NOT NULL,
    occurrences bigint NOT NULL DEFAULT 1,
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen timestamptz NOT NULL DEFAULT now(),
    UNIQUE(company_id,fingerprint,bucket),
    UNIQUE(company_id,id)
);
CREATE INDEX support_events_list ON support.events(company_id,last_seen DESC,id);
CREATE TABLE support.tickets (
    id uuid PRIMARY KEY,
    company_id text NOT NULL,
    company_name text NOT NULL,
    actor_id text NOT NULL,
    actor_name text NOT NULL DEFAULT 'Система',
    client_request_id uuid NOT NULL,
    payload_hash text NOT NULL,
    description text NOT NULL CHECK(length(description)<=4000),
    screen text NOT NULL,
    event_id uuid,
    status text NOT NULL DEFAULT 'new' CHECK(status IN ('new','investigating','resolved','dismissed')),
    revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(company_id,actor_id,client_request_id),
    UNIQUE(company_id,id),
    FOREIGN KEY(company_id,event_id) REFERENCES support.events(company_id,id) ON DELETE SET NULL (event_id)
);
CREATE INDEX support_tickets_list ON support.tickets(company_id,created_at DESC,id);
CREATE TABLE support.audit (
    id uuid PRIMARY KEY,
    company_id text NOT NULL,
    ticket_id uuid NOT NULL,
    actor_id text NOT NULL,
    actor_name text NOT NULL DEFAULT 'Система',
    from_status text,
    to_status text NOT NULL,
    note text NOT NULL CHECK(length(note)<=2000),
    revision integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(ticket_id,revision),
    FOREIGN KEY(company_id,ticket_id) REFERENCES support.tickets(company_id,id) ON DELETE CASCADE
);
CREATE TABLE support.event_receipts (
    company_id text NOT NULL,
    actor_id text NOT NULL,
    client_request_id uuid NOT NULL,
    event_id uuid NOT NULL,
    payload_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(company_id,actor_id,client_request_id),
    FOREIGN KEY(company_id,event_id) REFERENCES support.events(company_id,id) ON DELETE CASCADE
);
CREATE TABLE support.rate_buckets (
    company_id text NOT NULL,
    actor_id text NOT NULL,
    operation text NOT NULL,
    bucket timestamptz NOT NULL,
    n integer NOT NULL,
    PRIMARY KEY(company_id,actor_id,operation,bucket)
);
CREATE INDEX support_events_retention ON support.events(last_seen);
CREATE INDEX support_tickets_retention ON support.tickets(updated_at)
    WHERE status IN ('resolved','dismissed');
CREATE INDEX support_rates_retention ON support.rate_buckets(bucket);
CREATE INDEX support_receipts_retention ON support.event_receipts(created_at);
DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['events','tickets','audit','event_receipts','rate_buckets'] LOOP
        EXECUTE format('ALTER TABLE support.%I ENABLE ROW LEVEL SECURITY',t);
        EXECUTE format('ALTER TABLE support.%I FORCE ROW LEVEL SECURITY',t);
        EXECUTE format('CREATE POLICY company_bound ON support.%I FOR ALL USING (support.can_access(company_id)) WITH CHECK (support.can_access(company_id))',t);
        EXECUTE format('REVOKE ALL ON support.%I FROM PUBLIC,anon,authenticated',t);
    END LOOP;
END $$;
GRANT USAGE ON SCHEMA support TO restcontrol_backend;
GRANT EXECUTE ON FUNCTION support.can_access(text) TO restcontrol_backend;
GRANT SELECT,INSERT,UPDATE,DELETE ON support.events,support.tickets,support.audit,support.event_receipts,support.rate_buckets TO restcontrol_backend;
REVOKE UPDATE,DELETE ON support.audit FROM restcontrol_backend;
-- Restricted primary/tenant capture login is provisioned outside migration:
-- CREATE ROLE chaika_support LOGIN ...;
-- INSERT INTO support.principals(role_name,company_id) VALUES('chaika_support','primary-dashboard');
-- GRANT USAGE ON SCHEMA support TO chaika_support;
-- GRANT EXECUTE ON FUNCTION support.can_access(text) TO chaika_support;
-- GRANT SELECT,INSERT ON support.events,support.tickets,support.audit,support.event_receipts,support.rate_buckets TO chaika_support;
-- GRANT UPDATE(occurrences,last_seen,request_id) ON support.events TO chaika_support;
-- GRANT UPDATE(n) ON support.rate_buckets TO chaika_support;
COMMIT;
