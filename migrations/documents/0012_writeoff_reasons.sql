BEGIN;
-- Preserve document choices before enabling mutable reason settings.
-- Deploy snapshot-aware portal AND standalone worker before allowing reason edits.
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='60s';

ALTER TABLE chaika_iiko_documents.writeoffs_reasons
    ADD COLUMN version integer NOT NULL DEFAULT 1 CHECK (version > 0);
ALTER TABLE chaika_iiko_documents.writeoffs
    ADD COLUMN reason_pk bigint REFERENCES chaika_iiko_documents.writeoffs_reasons(id),
    ADD COLUMN reason_account_id uuid;

-- The existing name is the historical display snapshot, never cascade a rename.
UPDATE chaika_iiko_documents.writeoffs d SET reason_pk=r.id
FROM chaika_iiko_documents.writeoffs_reasons r WHERE r.name=d.reason_id;
-- A reserved dispatch is authoritative even if the dictionary changed meanwhile.
UPDATE chaika_iiko_documents.writeoffs d SET reason_account_id=(
    SELECT (j.payload->'json'->>'accountId')::uuid
    FROM chaika_iiko_documents.native_dispatch j
    WHERE j.kind='writeoff' AND j.document_id=d.id
    ORDER BY j.version DESC,j.created_at DESC LIMIT 1
);
-- Completed legacy documents without a payload have no recoverable account history.
UPDATE chaika_iiko_documents.writeoffs d SET reason_account_id=r.account_id
FROM chaika_iiko_documents.writeoffs_reasons r
WHERE d.reason_pk=r.id AND d.reason_account_id IS NULL
    AND d.status='Created' AND d.submission_state IN ('idle','failed')
    AND NOT EXISTS (SELECT 1 FROM chaika_iiko_documents.native_dispatch j
                    WHERE j.kind='writeoff' AND j.document_id=d.id);
-- Validate deferred legacy foreign keys before changing their table.
SET CONSTRAINTS ALL IMMEDIATE;
ALTER TABLE chaika_iiko_documents.writeoffs
    DROP CONSTRAINT writeoffs_reason_id_ca8d5b1b_fk_writeoffs_reasons_name;

-- Old writers still send only the name during the rolling deployment.
CREATE FUNCTION chaika_iiko_documents.snapshot_writeoff_reason() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_OP='INSERT' OR NEW.reason_id IS DISTINCT FROM OLD.reason_id THEN
        SELECT r.id,r.account_id INTO NEW.reason_pk,NEW.reason_account_id
        FROM chaika_iiko_documents.writeoffs_reasons r WHERE r.name=NEW.reason_id FOR SHARE;
        IF NEW.reason_id IS NOT NULL AND NEW.reason_pk IS NULL THEN
            RAISE foreign_key_violation USING MESSAGE='Unknown writeoff reason';
        END IF;
    END IF;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION chaika_iiko_documents.snapshot_writeoff_reason() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION chaika_iiko_documents.snapshot_writeoff_reason() TO chaika_iiko_app;
CREATE TRIGGER snapshot_writeoff_reason
BEFORE INSERT OR UPDATE OF reason_id ON chaika_iiko_documents.writeoffs
FOR EACH ROW EXECUTE FUNCTION chaika_iiko_documents.snapshot_writeoff_reason();

CREATE TABLE chaika_iiko_documents.writeoffs_reason_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reason_id bigint NOT NULL REFERENCES chaika_iiko_documents.writeoffs_reasons(id),
    actor_supabase_id uuid NOT NULL,
    version integer NOT NULL CHECK(version>0),
    data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT,INSERT,UPDATE ON chaika_iiko_documents.writeoffs_reasons TO chaika_iiko_app;
GRANT USAGE,SELECT ON SEQUENCE chaika_iiko_documents.writeoffs_reasons_id_seq TO chaika_iiko_app;
GRANT SELECT,INSERT ON chaika_iiko_documents.writeoffs_reason_events TO chaika_iiko_app;
GRANT USAGE,SELECT ON SEQUENCE chaika_iiko_documents.writeoffs_reason_events_id_seq TO chaika_iiko_app;

-- The analytics dictionary is optional in isolated document-only installations.
DO $$ BEGIN
    IF to_regclass('chaika.accounts') IS NOT NULL THEN
        GRANT USAGE ON SCHEMA chaika TO chaika_iiko_app;
        GRANT SELECT ON chaika.accounts TO chaika_iiko_app;
        DROP POLICY IF EXISTS documents_read ON chaika.accounts;
        CREATE POLICY documents_read ON chaika.accounts FOR SELECT
            TO chaika_iiko_app USING(source_id='primary');
    END IF;
END $$;
COMMIT;
