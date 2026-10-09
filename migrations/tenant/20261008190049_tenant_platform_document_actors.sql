-- Additive actor snapshots: global owners never need a local authentication_user.
-- Trusted application authentication supplies snapshots; SQL enforces their shape
-- and binds every snapshot to this schema's company UUID.
SET LOCAL search_path={documents},pg_catalog;

CREATE FUNCTION {documents}.native_actor_snapshot_valid(value jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path=pg_catalog
AS $actor$
 SELECT (
    jsonb_typeof(value) = 'object'
    AND value ?& ARRAY['company_id','auth_user_id','kind','display_name','membership_id']
    AND jsonb_typeof(value->'company_id') = 'string'
    AND value->>'company_id' =
        substring(trim(both '"' from '{documents}'),3,32)::uuid::text
    AND jsonb_typeof(value->'auth_user_id') = 'string'
    AND value->>'auth_user_id' ~ '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
    AND value->>'auth_user_id' <> '00000000-0000-0000-0000-000000000000'
    AND jsonb_typeof(value->'kind') = 'string'
    AND value->>'kind' IN ('platform_owner','company_member')
    AND jsonb_typeof(value->'display_name') = 'string'
    AND value->>'display_name' ~ '[^[:space:]]'
    AND (value->'membership_id' = 'null'::jsonb OR (
        jsonb_typeof(value->'membership_id') = 'string'
        AND value->>'membership_id' ~ '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
        AND value->>'membership_id' <> '00000000-0000-0000-0000-000000000000'
    ))
    AND (value->>'kind' <> 'platform_owner' OR value->'membership_id' = 'null'::jsonb)
 ) IS TRUE;
$actor$;
REVOKE ALL ON FUNCTION {documents}.native_actor_snapshot_valid(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {documents}.native_actor_snapshot_valid(jsonb) TO {runtime_role};

ALTER TABLE {documents}.waybills
    ADD COLUMN created_actor jsonb,
    ALTER COLUMN created_by_id DROP NOT NULL,
    ADD CONSTRAINT waybills_created_actor_valid CHECK (
        (created_actor IS NULL OR {documents}.native_actor_snapshot_valid(created_actor))
        AND (created_by_id IS NOT NULL OR (created_actor->>'kind' = 'platform_owner') IS TRUE)
    );

ALTER TABLE {documents}.waybills
    ADD COLUMN processed_actor jsonb,
    ADD CONSTRAINT waybills_processed_actor_valid CHECK (
        (processed_actor IS NULL OR {documents}.native_actor_snapshot_valid(processed_actor))
        AND (processed_by_id IS NOT NULL OR processed_actor IS NULL OR (processed_actor->>'kind' = 'platform_owner') IS TRUE)
    );

ALTER TABLE {documents}.writeoffs
    ADD COLUMN created_actor jsonb,
    ALTER COLUMN created_by_id DROP NOT NULL,
    ADD CONSTRAINT writeoffs_created_actor_valid CHECK (
        (created_actor IS NULL OR {documents}.native_actor_snapshot_valid(created_actor))
        AND (created_by_id IS NOT NULL OR (created_actor->>'kind' = 'platform_owner') IS TRUE)
    );

ALTER TABLE {documents}.writeoffs
    ADD COLUMN processed_actor jsonb,
    ADD CONSTRAINT writeoffs_processed_actor_valid CHECK (
        (processed_actor IS NULL OR {documents}.native_actor_snapshot_valid(processed_actor))
        AND (processed_by_id IS NOT NULL OR processed_actor IS NULL OR (processed_actor->>'kind' = 'platform_owner') IS TRUE)
    );

ALTER TABLE {documents}.commercial_invoices
    ADD COLUMN created_actor jsonb,
    ALTER COLUMN created_by_id DROP NOT NULL,
    ADD CONSTRAINT commercial_invoices_created_actor_valid CHECK (
        (created_actor IS NULL OR {documents}.native_actor_snapshot_valid(created_actor))
        AND (created_by_id IS NOT NULL OR (created_actor->>'kind' = 'platform_owner') IS TRUE)
    );

ALTER TABLE {documents}.portal_documents_event
    ADD COLUMN actor jsonb,
    ALTER COLUMN actor_id DROP NOT NULL,
    ADD CONSTRAINT portal_documents_event_actor_valid CHECK (
        (actor IS NULL OR {documents}.native_actor_snapshot_valid(actor))
        AND (actor_id IS NOT NULL OR (actor->>'kind' = 'platform_owner') IS TRUE)
    );

ALTER TABLE {documents}.portal_documents_operation
    ADD COLUMN actor jsonb,
    ALTER COLUMN actor_id DROP NOT NULL,
    ADD CONSTRAINT portal_documents_operation_actor_valid CHECK (
        (actor IS NULL OR {documents}.native_actor_snapshot_valid(actor))
        AND (actor_id IS NOT NULL OR (actor->>'kind' = 'platform_owner') IS TRUE)
    );

ALTER TABLE {documents}.commercial_invoice_operations
    ADD COLUMN actor jsonb,
    ALTER COLUMN actor_id DROP NOT NULL,
    ADD CONSTRAINT commercial_invoice_operations_actor_valid CHECK (
        (actor IS NULL OR {documents}.native_actor_snapshot_valid(actor))
        AND (actor_id IS NOT NULL OR (actor->>'kind' = 'platform_owner') IS TRUE)
    );

ALTER TABLE {documents}.commercial_invoice_events
    ADD COLUMN actor jsonb,
    ADD CONSTRAINT commercial_invoice_events_actor_valid CHECK (
        (actor IS NULL OR {documents}.native_actor_snapshot_valid(actor))
        AND (actor_id IS NOT NULL OR actor IS NULL OR (actor->>'kind' = 'platform_owner') IS TRUE)
    );

ALTER TABLE {documents}.commercial_counterparty_operations
    ADD COLUMN actor jsonb,
    ALTER COLUMN actor_id DROP NOT NULL,
    ADD CONSTRAINT commercial_counterparty_operations_actor_valid CHECK (
        (actor IS NULL OR {documents}.native_actor_snapshot_valid(actor))
        AND (actor_id IS NOT NULL OR (actor->>'kind' = 'platform_owner') IS TRUE)
    );

CREATE TABLE {documents}.native_actor_audit (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL CHECK (
        company_id = substring(trim(both '"' from '{documents}'),3,32)::uuid
    ),
    actor_uuid uuid NOT NULL CHECK (actor_uuid <> '00000000-0000-0000-0000-000000000000'::uuid),
    actor_kind text NOT NULL CHECK (actor_kind IN ('platform_owner','company_member')),
    actor_display_name text NOT NULL CHECK (actor_display_name ~ '[^[:space:]]'),
    action text NOT NULL CHECK (length(btrim(action)) > 0),
    object_kind text NOT NULL CHECK (length(btrim(object_kind)) > 0),
    object_id text,
    data jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK (jsonb_typeof(data) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX native_actor_audit_object ON {documents}.native_actor_audit(object_kind,object_id,created_at);
REVOKE ALL ON {documents}.native_actor_audit FROM PUBLIC, {runtime_role};
GRANT SELECT,INSERT ON {documents}.native_actor_audit TO {runtime_role};
-- The provisioning runner enables/FORCEs RLS and installs its tenant-role policy.
