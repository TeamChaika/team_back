-- Private tenant template. Apply only through app.tenancy.migrations.
-- Source: 0001_native_runtime.sql
-- Existing documents, identities, grants and sequences remain untouched.
-- Run as chaika_iiko_migrator, never as the web application role.

SET LOCAL search_path={documents},pg_catalog;
CREATE TABLE IF NOT EXISTS native_catalog (
    name text PRIMARY KEY,
    data jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS native_jobs (
    name text PRIMARY KEY,
    data jsonb NOT NULL DEFAULT '{{}}',
    updated_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT,INSERT,UPDATE ON native_catalog,native_jobs TO {runtime_role};
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
GRANT SELECT,INSERT,UPDATE ON native_dispatch TO {runtime_role};
CREATE TABLE IF NOT EXISTS native_bot_updates (
    id bigint PRIMARY KEY,
    data jsonb NOT NULL,
    state text NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT,INSERT,UPDATE ON native_bot_updates TO {runtime_role};

-- Source: 0002_telegram_link.sql
-- Run as chaika_iiko_migrator before deploying self-service profile endpoints.
-- No existing identity, document, warehouse grant or history is changed.

SET LOCAL search_path={documents},pg_catalog;
CREATE TABLE IF NOT EXISTS native_telegram_link (
    token_hash text PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{{64}}$'),
    portal_id uuid NOT NULL UNIQUE,
    user_id bigint NOT NULL REFERENCES authentication_user(id) ON DELETE CASCADE,
    revision integer NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
-- UPDATE is required by SELECT ... FOR UPDATE during atomic consumption.
GRANT SELECT,INSERT,UPDATE,DELETE ON native_telegram_link TO {runtime_role};

-- Source: 0003_password_recovery.sql
-- Apply as chaika_iiko_migrator before recovery code/worker rollout.

SET LOCAL search_path={documents},pg_catalog;
CREATE TABLE IF NOT EXISTS native_password_recovery (
    token_hash text PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{{64}}$'),
    portal_id uuid NOT NULL UNIQUE,
    user_id bigint NOT NULL REFERENCES authentication_user(id) ON DELETE CASCADE,
    telegram_id bigint NOT NULL,
    revision integer NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    claimed_at timestamptz
);
GRANT SELECT,INSERT,UPDATE,DELETE ON native_password_recovery TO {runtime_role};

-- Source: 0004_receipt_discrepancies.sql
-- Apply before starting the updated portal and document worker.

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
SET LOCAL search_path={documents},pg_catalog;
ALTER TABLE waybills ADD COLUMN IF NOT EXISTS receipt_state text NOT NULL DEFAULT 'none'
    CHECK (receipt_state IN ('none','pending_sender','accepted','rejected'));
ALTER TABLE waybills_items ADD COLUMN IF NOT EXISTS received_amount double precision
    CHECK (received_amount >= 0 AND received_amount <= 1e9);

-- Source: 0005_writeoff_cost_estimates.sql
-- Apply before deploying the updated portal and worker; no historical backfill.

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
ALTER TABLE {documents}.writeoffs ADD COLUMN IF NOT EXISTS cost_estimate jsonb;
-- Standalone document test databases may not contain the analytics schema.
DO $$
BEGIN
    IF to_regclass('{analytics}.store_balance_reports') IS NOT NULL
       AND to_regclass('{analytics}.store_balance_items') IS NOT NULL THEN
        GRANT USAGE ON SCHEMA {analytics} TO {runtime_role};
        GRANT SELECT ON {analytics}.store_balance_reports,{analytics}.store_balance_items TO {runtime_role};
        DROP POLICY IF EXISTS documents_cost_reports ON {analytics}.store_balance_reports;
        CREATE POLICY documents_cost_reports ON {analytics}.store_balance_reports
            FOR SELECT TO {runtime_role} USING (source_id='primary');
        DROP POLICY IF EXISTS documents_cost_items ON {analytics}.store_balance_items;
        CREATE POLICY documents_cost_items ON {analytics}.store_balance_items
            FOR SELECT TO {runtime_role} USING (EXISTS (
                SELECT 1 FROM {analytics}.store_balance_reports r
                WHERE r.source_id='primary' AND r.last_snapshot_id=snapshot_id
            ));
    END IF;
END $$;

-- Source: 0006_writeoff_recipe_costs.sql
-- Apply before portal/worker deployment. Read-only recipe/reference access, primary only.

SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['products','stores','corporate_nodes',
        'assembly_charts','assembly_chart_items','assembly_chart_scopes']
    LOOP
        IF to_regclass('{analytics}.' || t) IS NOT NULL THEN
            GRANT USAGE ON SCHEMA {analytics} TO {runtime_role};
            EXECUTE format('GRANT SELECT ON {analytics}.%I TO {runtime_role}', t);
            EXECUTE format('DROP POLICY IF EXISTS documents_recipe_cost ON {analytics}.%I', t);
            EXECUTE format('CREATE POLICY documents_recipe_cost ON {analytics}.%I '
                'FOR SELECT TO {runtime_role} USING (source_id=''primary'')', t);
        END IF;
    END LOOP;
END $$;

-- Source: 0007_telegram_cleanup.sql
-- Additive Telegram delivery ledger; document records/history remain untouched.

SET LOCAL search_path={documents},pg_catalog;
CREATE TABLE IF NOT EXISTS native_telegram_cleanup (
    kind text NOT NULL CHECK (kind IN ('waybill','writeoff')),
    document_id bigint NOT NULL,
    approved_version integer NOT NULL,
    PRIMARY KEY (kind,document_id)
);
CREATE TABLE IF NOT EXISTS native_telegram_messages (
    chat_id bigint NOT NULL,
    message_id bigint NOT NULL,
    kind text NOT NULL CHECK (kind IN ('waybill','writeoff')),
    document_id bigint NOT NULL,
    version integer NOT NULL,
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','pending','deleted','buttons_removed','unavailable')),
    attempts integer NOT NULL DEFAULT 0,
    remove_buttons boolean NOT NULL DEFAULT false,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id,message_id)
);
CREATE INDEX IF NOT EXISTS native_telegram_messages_due ON native_telegram_messages(next_attempt_at) WHERE state='pending';
CREATE INDEX IF NOT EXISTS native_telegram_messages_document ON native_telegram_messages(kind,document_id,version);
-- Empty baseline intentionally omits legacy Telegram history backfill.
GRANT SELECT,INSERT,UPDATE ON native_telegram_cleanup,native_telegram_messages TO {runtime_role};
