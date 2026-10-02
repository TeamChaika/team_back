-- Existing documents, identities, grants and sequences remain untouched.
-- Run as chaika_iiko_migrator, never as the web application role.
BEGIN;
SET LOCAL search_path=chaika_iiko_documents,pg_catalog;
CREATE TABLE IF NOT EXISTS native_catalog (
    name text PRIMARY KEY,
    data jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS native_jobs (
    name text PRIMARY KEY,
    data jsonb NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT,INSERT,UPDATE ON native_catalog,native_jobs TO chaika_iiko_app;
CREATE TABLE IF NOT EXISTS native_dispatch (
    operation_id uuid PRIMARY KEY REFERENCES portal_documents_operation(id),
    kind text NOT NULL CHECK (kind IN ('waybill','writeoff')),
    document_id bigint NOT NULL,
    version integer NOT NULL,
    payload jsonb NOT NULL,
    state text NOT NULL DEFAULT 'ready',
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    claim_id uuid,
    claimed_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(kind,document_id,version)
);
CREATE INDEX IF NOT EXISTS native_dispatch_due ON native_dispatch (next_attempt_at) WHERE state IN ('ready','connecting');
GRANT SELECT,INSERT,UPDATE ON native_dispatch TO chaika_iiko_app;
CREATE TABLE IF NOT EXISTS native_bot_updates (
    id bigint PRIMARY KEY,
    data jsonb NOT NULL,
    state text NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT,INSERT,UPDATE ON native_bot_updates TO chaika_iiko_app;
COMMIT;
