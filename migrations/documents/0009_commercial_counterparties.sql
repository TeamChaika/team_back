-- Explicit global counterparty creation. No existing permissions are expanded.
BEGIN;
SET LOCAL search_path=chaika_iiko_documents,pg_catalog;
SET LOCAL lock_timeout='5s';
CREATE TABLE IF NOT EXISTS commercial_counterparty_grants (
 user_id bigint PRIMARY KEY REFERENCES authentication_user(id),
 can_create boolean NOT NULL DEFAULT false, revision integer NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS commercial_counterparty_grant_operations (
 request_id uuid PRIMARY KEY, portal_id uuid NOT NULL, fingerprint text NOT NULL,
 user_id bigint NOT NULL REFERENCES authentication_user(id),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS commercial_counterparty_operations (
 id uuid PRIMARY KEY, actor_id bigint NOT NULL REFERENCES authentication_user(id), portal_id uuid NOT NULL,
 kind text NOT NULL CHECK(kind IN ('purchase','sale')), fingerprint text NOT NULL,
 source_key text NOT NULL, iiko_id uuid NOT NULL UNIQUE, code text NOT NULL,
 payload jsonb NOT NULL, identity_key text NOT NULL,
 state text NOT NULL DEFAULT 'queued' CHECK(state IN
 ('queued','connecting','sending','confirmed','rejected','unknown')),
 claim_id uuid, attempts integer NOT NULL DEFAULT 0,
 next_attempt_at timestamptz NOT NULL DEFAULT now(), error_code text, candidates jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS commercial_counterparty_due
 ON commercial_counterparty_operations(next_attempt_at) WHERE state IN
 ('queued','connecting','sending','unknown');
CREATE INDEX IF NOT EXISTS commercial_counterparty_identity
 ON commercial_counterparty_operations(source_key,identity_key);
CREATE TABLE IF NOT EXISTS commercial_counterparties (
 id uuid PRIMARY KEY, source_key text NOT NULL, data jsonb NOT NULL,
 operation_id uuid NOT NULL UNIQUE REFERENCES commercial_counterparty_operations(id),
 verified_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS commercial_counterparty_events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 operation_id uuid NOT NULL REFERENCES commercial_counterparty_operations(id),
 state text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT,INSERT,UPDATE ON commercial_counterparty_grants,
 commercial_counterparty_operations,commercial_counterparties TO chaika_iiko_app;
GRANT SELECT,INSERT ON commercial_counterparty_grant_operations,
 commercial_counterparty_events TO chaika_iiko_app;
GRANT USAGE,SELECT ON SEQUENCE commercial_counterparty_events_id_seq TO chaika_iiko_app;
COMMIT;
