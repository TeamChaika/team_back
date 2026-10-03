-- Apply as chaika_iiko_migrator before recovery code/worker rollout.
BEGIN;
SET LOCAL search_path=chaika_iiko_documents,pg_catalog;
CREATE TABLE IF NOT EXISTS native_password_recovery (
    token_hash text PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    portal_id uuid NOT NULL UNIQUE,
    user_id bigint NOT NULL REFERENCES authentication_user(id) ON DELETE CASCADE,
    telegram_id bigint NOT NULL,
    revision integer NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    claimed_at timestamptz
);
GRANT SELECT,INSERT,UPDATE,DELETE ON native_password_recovery TO chaika_iiko_app;
COMMIT;
