-- Payload offload retains immutable IDs, provenance and business-table foreign keys.
SET LOCAL lock_timeout='3s';
ALTER TABLE {analytics}.raw_snapshots
    ALTER COLUMN raw DROP NOT NULL,
    ALTER COLUMN normalized DROP NOT NULL,
    ADD COLUMN payload_object_key text,
    ADD COLUMN payload_object_sha256 text,
    ADD COLUMN payload_object_bytes bigint,
    ADD COLUMN payload_format_version integer,
    ADD COLUMN payload_verified_at timestamptz,
    ADD CONSTRAINT raw_snapshots_payload_storage_check CHECK (
        (raw IS NOT NULL AND normalized IS NOT NULL
         AND payload_object_key IS NULL AND payload_object_sha256 IS NULL
         AND payload_object_bytes IS NULL AND payload_format_version IS NULL
         AND payload_verified_at IS NULL)
        OR
        (raw IS NULL AND normalized IS NULL
         AND payload_object_key IS NOT NULL AND payload_object_key <> ''
         AND payload_object_sha256 IS NOT NULL
         AND payload_object_sha256 ~ '^[0-9a-f]{{64}}$'
         AND payload_object_bytes IS NOT NULL AND payload_object_bytes > 0
         AND payload_format_version IS NOT NULL AND payload_format_version = 1
         AND payload_verified_at IS NOT NULL)
    ) NOT VALID;
-- Validate in a separate operator transaction after rollout, avoiding a full scan
-- while the column ALTER lock is held. NOT VALID still checks every new/updated row.
-- ALTER TABLE {analytics}.raw_snapshots VALIDATE CONSTRAINT raw_snapshots_payload_storage_check;
-- Existing length/hash checks continue protecting inline rows. SQL NULL makes
-- them non-applicable to external rows; application verifies external payloads.
-- Runtime SELECT/INSERT ACL and RLS are unchanged. Backfill uses an owner role.
