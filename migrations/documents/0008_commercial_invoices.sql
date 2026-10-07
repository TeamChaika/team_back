-- Separate priced invoices. No grant expansion, existing waybills stay untouched.
BEGIN;
SET LOCAL search_path=chaika_iiko_documents,pg_catalog;
SET LOCAL lock_timeout='5s';
CREATE SEQUENCE IF NOT EXISTS commercial_invoice_number_seq;
CREATE TABLE IF NOT EXISTS commercial_invoice_grants (
 user_id bigint NOT NULL REFERENCES authentication_user(id),
 kind text NOT NULL CHECK(kind IN ('purchase','sale')),
 store_id uuid NOT NULL REFERENCES stores(id), actions jsonb NOT NULL,
 PRIMARY KEY(user_id,kind,store_id), CHECK(jsonb_typeof(actions)='array')
);
GRANT SELECT,INSERT,UPDATE,DELETE ON commercial_invoice_grants TO chaika_iiko_app;
CREATE TABLE IF NOT EXISTS commercial_grant_revisions (
 user_id bigint PRIMARY KEY REFERENCES authentication_user(id), revision integer NOT NULL
);
CREATE TABLE IF NOT EXISTS commercial_grant_operations (
 request_id uuid PRIMARY KEY,portal_id uuid NOT NULL,
 fingerprint text NOT NULL,user_id bigint NOT NULL REFERENCES authentication_user(id),
 created_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT,INSERT,UPDATE ON commercial_grant_revisions TO chaika_iiko_app;
GRANT SELECT,INSERT ON commercial_grant_operations TO chaika_iiko_app;
CREATE TABLE IF NOT EXISTS commercial_invoices (
 id uuid PRIMARY KEY, kind text NOT NULL CHECK(kind IN ('purchase','sale')),
 number text NOT NULL UNIQUE, store_id uuid NOT NULL REFERENCES stores(id),
 created_by_id bigint NOT NULL REFERENCES authentication_user(id),
 version integer NOT NULL DEFAULT 1 CHECK(version>0),
 state text NOT NULL DEFAULT 'draft' CHECK(state IN
 ('draft','queued','sending','accepted','processed','rejected','unknown')),
 snapshot jsonb NOT NULL,
 iiko_id uuid,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS commercial_invoices_store ON commercial_invoices(kind,store_id,created_at DESC);
CREATE TABLE IF NOT EXISTS commercial_invoice_revisions (
 document_id uuid NOT NULL REFERENCES commercial_invoices(id),version integer NOT NULL,
 snapshot jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(document_id,version)
);
CREATE TABLE IF NOT EXISTS commercial_invoice_operations (
 request_id uuid PRIMARY KEY, actor_id bigint NOT NULL REFERENCES authentication_user(id),
 fingerprint text NOT NULL,document_id uuid NOT NULL REFERENCES commercial_invoices(id),
 result jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS commercial_invoice_events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 document_id uuid NOT NULL REFERENCES commercial_invoices(id),version integer NOT NULL,
 actor_id bigint REFERENCES authentication_user(id),action text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS commercial_invoice_events_document ON commercial_invoice_events(document_id,id);
CREATE TABLE IF NOT EXISTS commercial_invoice_dispatch (
 document_id uuid PRIMARY KEY REFERENCES commercial_invoices(id),version integer NOT NULL CHECK(version>0),
 payload jsonb NOT NULL,state text NOT NULL DEFAULT 'ready' CHECK(state IN
 ('ready','connecting','sending','accepted','processed','rejected','unknown','obsolete')),
 attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
 claim_id uuid,claimed_at timestamptz,next_attempt_at timestamptz NOT NULL DEFAULT now(),
 last_error text,created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS commercial_invoice_dispatch_due ON commercial_invoice_dispatch(next_attempt_at)
 WHERE state IN ('ready','connecting');
GRANT SELECT,INSERT,UPDATE ON commercial_invoices,commercial_invoice_dispatch TO chaika_iiko_app;
GRANT SELECT,INSERT ON commercial_invoice_revisions,commercial_invoice_operations,commercial_invoice_events TO chaika_iiko_app;
GRANT USAGE,SELECT ON SEQUENCE commercial_invoice_number_seq,commercial_invoice_events_id_seq TO chaika_iiko_app;
COMMIT;
